# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Helpers for running each UAT notebook as an isolated Kubernetes Job.

This module holds the notebook discovery, selection, per-notebook Job lifecycle, and
result parsing used by the driver's parametrised notebook test. Keeping these pieces
here makes the test body thin and lets each piece be reasoned about (and unit-tested)
independently.

The reliable channel for a notebook's outcome is its **logs**: the in-cluster runner
prints a machine-readable result marker (and, when artifacts are kept, gzip+base64
blobs) to stdout, which ``kubectl logs`` retrieves even from a completed pod. This
avoids any dependency on shared (RWX) storage, keeping the suite portable.
"""

import base64
import gzip
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import tenacity
from lightkube import ApiError, Client, codecs
from lightkube.generic_resource import create_global_resource
from lightkube.resources.batch_v1 import Job
from lightkube.types import CascadeType

log = logging.getLogger(__name__)

RUNTIMECLASS_RESOURCE = create_global_resource(
    group="node.k8s.io",
    version="v1",
    kind="runtimeclass",
    plural="runtimeclasses",
)

# Markers emitted by the in-cluster runner and parsed from the Job logs.
_RESULT_RE = re.compile(r"===UAT-RESULT===(?P<payload>.*?)===END-UAT-RESULT===", re.DOTALL)
_ARTIFACT_RE = re.compile(
    r"===UAT-ARTIFACT:(?P<name>[^=]+)===\n(?P<blob>.*?)\n===END-UAT-ARTIFACT===",
    re.DOTALL,
)


@dataclass
class NotebookResult:
    """Outcome of running a single notebook as a Kubernetes Job."""

    name: str
    status: str  # "PASSED", "FAILED", or "TIMEOUT"
    duration: float = 0.0
    failing_cell: Optional[int] = None
    error_summary: str = ""
    log_tail: str = ""
    artifacts_dir: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        """Return whether the notebook run passed."""
        return self.status == "PASSED"


def discover_notebooks(directory: str) -> Dict[str, str]:
    """Return a sorted mapping of notebook stem -> absolute path under ``directory``.

    Directories holding IPYNB checkpoints are ignored. A missing ``directory`` yields
    an empty mapping.
    """
    notebooks: Dict[str, str] = {}
    for root, dirs, files in os.walk(directory):
        dirs[:] = [entry for entry in dirs if entry != ".ipynb_checkpoints"]
        for file_name in files:
            if file_name.endswith(".ipynb"):
                stem = file_name[: -len(".ipynb")]
                notebooks[stem] = os.path.abspath(os.path.join(root, file_name))
    return dict(sorted(notebooks.items()))


def notebook_matches_filter(notebook_name: str, filter_expr: str) -> bool:
    """Return whether ``notebook_name`` matches a pytest ``-k`` style ``filter_expr``.

    Uses pytest's own expression engine so that ``and``/``or``/``not`` behave exactly
    like ``-k``, falling back to a plain substring match if the expression cannot be
    compiled.
    """
    if not filter_expr:
        return True
    words = set(re.split(r"[^0-9A-Za-z]+", notebook_name))
    words.add(notebook_name)
    try:
        from _pytest.mark.expression import Expression

        return bool(Expression.compile(filter_expr).evaluate(lambda word: word in words))
    except Exception:
        return filter_expr in notebook_name


def job_name_for(prefix: str, notebook_name: str) -> str:
    """Return a DNS-1123-compliant Job name for ``notebook_name``."""
    slug = re.sub(r"[^a-z0-9-]+", "-", notebook_name.lower()).strip("-")
    name = f"{prefix}-{slug}"
    if len(name) > 63:
        digest = hashlib.sha1(notebook_name.encode()).hexdigest()[:8]
        name = f"{name[:54]}-{digest}"
    return name


def render_notebook_job(template_path: str, context: dict):
    """Render the notebook Job template with ``context`` and return the manifest."""
    manifests = list(codecs.load_all_yaml(Path(template_path).read_text(), context=context))
    if len(manifests) != 1:
        raise ValueError(f"Expected 1 Job, got {len(manifests)}!")
    return manifests[0]


def record_result(config, result: NotebookResult) -> None:
    """Store a notebook result on the pytest config for the terminal summary."""
    store = getattr(config, "_notebook_results", None)
    if store is None:
        store = {}
        config._notebook_results = store
    store[result.name] = result


def _terminal_status(job) -> Optional[str]:
    """Return the Job's terminal status, or None if it is still running."""
    status = job.status
    if status is None:
        return None
    if status.succeeded:
        return "PASSED"
    if status.failed:
        for condition in status.conditions or []:
            if condition.type == "Failed" and condition.reason == "DeadlineExceeded":
                return "TIMEOUT"
        return "FAILED"
    return None


def _wait_for_terminal_status(client: Client, job_name: str, namespace: str, timeout: int) -> str:
    """Poll a Job until it reaches a terminal status or the wait budget elapses."""
    retryer = tenacity.Retrying(
        wait=tenacity.wait_fixed(10),
        retry=tenacity.retry_if_result(lambda result: result is None),
        stop=tenacity.stop_after_delay(timeout + 300),
        reraise=True,
    )

    def _check() -> Optional[str]:
        job = client.get(Job, name=job_name, namespace=namespace)
        return _terminal_status(job)

    try:
        return retryer(_check)
    except tenacity.RetryError:
        return "TIMEOUT"


def _job_logs(job_name: str, namespace: str) -> str:
    """Return the combined logs of a Job's pod (works on completed pods)."""
    result = subprocess.run(
        ["kubectl", "logs", "-n", namespace, f"job/{job_name}", "--tail=-1"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout or ""


def _tail(text: str, lines: int) -> str:
    """Return the last ``lines`` lines of ``text``."""
    return "\n".join(text.splitlines()[-lines:])


def _parse_payload(logs: str) -> dict:
    """Return the structured result payload emitted by the runner, or an empty dict."""
    match = _RESULT_RE.search(logs)
    if not match:
        return {}
    try:
        return json.loads(match.group("payload").strip())
    except json.JSONDecodeError:
        return {}


def _extract_artifacts(logs: str, dest_dir: Path) -> None:
    """Decode gzip+base64 artifact blobs from ``logs`` into ``dest_dir``."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for match in _ARTIFACT_RE.finditer(logs):
        name = match.group("name").strip()
        try:
            data = gzip.decompress(base64.b64decode(match.group("blob")))
        except (ValueError, OSError) as error:
            log.warning(f"Failed to decode artifact {name}: {error}")
            continue
        (dest_dir / name).write_bytes(data)


def _delete_job(client: Client, job_name: str, namespace: str) -> None:
    """Delete a Job (and its pods), tolerating a missing one."""
    try:
        client.delete(Job, name=job_name, namespace=namespace, cascade=CascadeType.FOREGROUND)
    except ApiError as error:
        if error.status.code != 404:
            raise


def run_notebook_job(
    client: Client,
    notebook_name: str,
    manifest,
    namespace: str,
    timeout: int,
    keep_artifacts: bool,
    artifacts_root: str,
) -> NotebookResult:
    """Create the Job for one notebook, wait for it, and return its result.

    The notebook's logs carry the structured result and, when ``keep_artifacts`` is
    set, the base64-encoded artifacts. The Job is deleted unless ``keep_artifacts`` is
    set (in which case it is left in the cluster for inspection).
    """
    job_name = manifest.metadata.name
    log.info(f"Running notebook '{notebook_name}' as Job {namespace}/{job_name}...")
    start = time.monotonic()
    client.create(manifest, namespace=namespace)
    job_status = _wait_for_terminal_status(client, job_name, namespace, timeout)
    duration = time.monotonic() - start

    logs = _job_logs(job_name, namespace)
    payload = _parse_payload(logs)
    # A deadline kill always wins; otherwise trust the runner's own status if present.
    status = job_status if job_status == "TIMEOUT" else payload.get("status", job_status)
    result = NotebookResult(
        name=notebook_name,
        status=status,
        duration=duration,
        failing_cell=payload.get("failing_cell"),
        error_summary=payload.get("error", "") or (_tail(logs, 20) if status != "PASSED" else ""),
        log_tail=_tail(logs, 20),
    )

    if keep_artifacts:
        dest = Path(artifacts_root) / notebook_name
        _extract_artifacts(logs, dest)
        result.artifacts_dir = str(dest)
    else:
        _delete_job(client, job_name, namespace)

    log.info(f"Notebook '{notebook_name}' finished: {result.status} in {duration:.0f}s")
    return result
