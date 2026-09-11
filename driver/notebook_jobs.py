# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Helpers for running each UAT notebook as an isolated Kubernetes Job.

This module holds the notebook discovery, selection, per-notebook Job lifecycle, and
result parsing used by the driver's parametrised notebook test. Keeping these pieces
here makes the test body thin and lets each piece be reasoned about (and unit-tested)
independently.

A notebook's outcome is the **Job's status** (driven by the pod's exit code): success →
PASSED, a non-zero exit → FAILED, and a Job deadline → TIMEOUT. On failure the driver
saves the pod logs to a file for debugging. This needs no shared (RWX) storage, keeping
the suite portable.
"""

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Dict, Optional

import tenacity
from lightkube import ApiError, Client, codecs
from lightkube.generic_resource import create_global_resource
from lightkube.resources.batch_v1 import Job
from lightkube.resources.core_v1 import Pod
from lightkube.types import CascadeType

log = logging.getLogger(__name__)

RUNTIMECLASS_RESOURCE = create_global_resource(
    group="node.k8s.io",
    version="v1",
    kind="runtimeclass",
    plural="runtimeclasses",
)


class NotebookStatus(StrEnum):
    """Terminal status reported for a single notebook run."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"


@dataclass
class NotebookResult:
    """Outcome of running a single notebook as a Kubernetes Job."""

    name: str
    status: NotebookStatus
    duration: float = 0.0
    log_file: Optional[str] = None
    logs: str = ""

    @property
    def succeeded(self) -> bool:
        """Return whether the notebook run passed."""
        return self.status == NotebookStatus.PASSED


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


def _terminal_status(job) -> Optional[NotebookStatus]:
    """Return the Job's terminal status, or None if it is still running."""
    status = job.status
    if status is None:
        return None
    if status.succeeded:
        return NotebookStatus.PASSED
    if status.failed:
        for condition in status.conditions or []:
            if condition.type == "Failed" and condition.reason == "DeadlineExceeded":
                return NotebookStatus.TIMEOUT
        return NotebookStatus.FAILED
    return None


def _wait_for_terminal_status(
    client: Client, job_name: str, namespace: str, timeout: int
) -> NotebookStatus:
    """Poll a Job until it reaches a terminal status or the wait budget elapses."""
    retryer = tenacity.Retrying(
        wait=tenacity.wait_fixed(10),
        retry=tenacity.retry_if_result(lambda result: result is None),
        stop=tenacity.stop_after_delay(timeout + 300),
        reraise=True,
    )

    def _check() -> Optional[NotebookStatus]:
        job = client.get(Job, name=job_name, namespace=namespace)
        return _terminal_status(job)

    try:
        return retryer(_check)
    except tenacity.RetryError:
        return NotebookStatus.TIMEOUT


def _job_logs(client: Client, job_name: str, namespace: str) -> str:
    """Return the logs of a Job's pod (works on completed pods)."""
    logs = []
    for pod in client.list(Pod, namespace=namespace, labels={"job-name": job_name}):
        try:
            logs.extend(client.log(pod.metadata.name, namespace=namespace, container=job_name))
        except ApiError as error:
            log.warning(f"Could not fetch logs for pod {pod.metadata.name}: {error}")
    return "".join(logs)


def _save_logs(logs: str, notebook_name: str, dest_root: str) -> Path:
    """Write a notebook's Job logs to ``<dest_root>/<notebook_name>.log``; return the path."""
    dest_dir = Path(dest_root)
    dest_dir.mkdir(parents=True, exist_ok=True)
    log_path = dest_dir / f"{notebook_name}.log"
    log_path.write_text(logs)
    return log_path


def _delete_job(client: Client, job_name: str, namespace: str) -> None:
    """Delete a Job (and its pods), tolerating a missing one."""
    try:
        client.delete(Job, name=job_name, namespace=namespace, cascade=CascadeType.FOREGROUND)
    except ApiError as error:
        if error.status.code != 404:
            raise


def _wait_for_pod_start(
    client: Client, job_name: str, namespace: str, timeout: int
) -> Optional[str]:
    """Return the Job pod's name once its container has started, or None on timeout."""

    def _started(pod) -> bool:
        statuses = pod.status.containerStatuses if pod.status else None
        return any(
            cs.name == job_name and (cs.state.running or cs.state.terminated)
            for cs in statuses or []
        )

    def _check() -> Optional[str]:
        for pod in client.list(Pod, namespace=namespace, labels={"job-name": job_name}):
            if _started(pod):
                return pod.metadata.name
        return None

    retryer = tenacity.Retrying(
        wait=tenacity.wait_fixed(2),
        retry=tenacity.retry_if_result(lambda result: result is None),
        stop=tenacity.stop_after_delay(timeout + 300),
    )
    try:
        return retryer(_check)
    except tenacity.RetryError:
        return None


def _pod_finished(client: Client, pod_name: str, namespace: str) -> bool:
    """Return whether the pod has reached a terminal phase (or no longer exists)."""
    try:
        pod = client.get(Pod, name=pod_name, namespace=namespace)
    except ApiError:
        return True
    phase = pod.status.phase if pod.status else None
    return phase in ("Succeeded", "Failed")


def _stream_pod_logs(
    client: Client, job_name: str, namespace: str, notebook_name: str, timeout: int
) -> str:
    """Follow the Job pod's logs live, printing each line, and return the full text.

    Best-effort: the follow connection can drop while the pod idles (read timeout), so it
    reconnects and resumes, skipping already-shown lines, until the pod finishes. Returns
    "" if the logs were not captured cleanly, so the caller can fall back to a plain fetch.
    """
    pod_name = _wait_for_pod_start(client, job_name, namespace, timeout)
    if not pod_name:
        return ""
    log.info(f"Streaming logs for '{notebook_name}' (pod {pod_name})...")
    collected = []
    shown = 0
    deadline = time.monotonic() + timeout + 300
    while time.monotonic() < deadline:
        try:
            for index, line in enumerate(
                client.log(pod_name, namespace=namespace, container=job_name, follow=True)
            ):
                if index < shown:
                    continue  # already printed before a reconnect (logs are append-only)
                # print (not log) keeps the notebook's raw output; pytest -s surfaces it live.
                print(line, end="", flush=True)
                collected.append(line)
                shown = index + 1
        except ApiError:
            return ""  # pod or its logs are gone -> caller fetches
        except Exception as error:
            if _pod_finished(client, pod_name, namespace):
                return ""  # finished but the stream dropped -> caller fetches complete logs
            log.debug(f"Reconnecting log stream for '{notebook_name}': {error}")
            time.sleep(1)
            continue  # transient drop (e.g. idle read timeout): reconnect and resume
        else:
            return "".join(collected)  # stream closed cleanly -> pod finished
    return ""


def run_notebook_job(
    client: Client,
    notebook_name: str,
    manifest,
    namespace: str,
    timeout: int,
    keep_artifacts: bool,
    artifacts_root: str,
) -> NotebookResult:
    """Create the Job for one notebook, stream its logs live, and return its result.

    The outcome is the Job's status: PASSED (exit 0), FAILED (non-zero exit), or TIMEOUT
    (Job deadline). The pod logs are streamed to stdout while it runs and, on failure,
    saved to a file. The Job is deleted unless ``keep_artifacts`` is set (kept for
    inspection).
    """
    job_name = manifest.metadata.name
    log.info(f"Running notebook '{notebook_name}' as Job {namespace}/{job_name}...")
    start = time.monotonic()
    client.create(manifest, namespace=namespace)
    # Follow the pod logs live (shown by pytest -s); this blocks until the pod terminates.
    logs = _stream_pod_logs(client, job_name, namespace, notebook_name, timeout)
    status = _wait_for_terminal_status(client, job_name, namespace, timeout)
    duration = time.monotonic() - start

    result = NotebookResult(name=notebook_name, status=status, duration=duration)
    if status != NotebookStatus.PASSED:
        logs = logs or _job_logs(client, job_name, namespace)
        result.log_file = str(_save_logs(logs, notebook_name, artifacts_root))
        result.logs = logs

    if not keep_artifacts:
        _delete_job(client, job_name, namespace)

    log.info(f"Notebook '{notebook_name}' finished: {result.status} in {duration:.0f}s")
    return result
