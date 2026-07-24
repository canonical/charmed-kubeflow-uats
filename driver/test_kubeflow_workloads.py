# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import contextlib
import logging
import os
import re
import subprocess
import sys
import time
from functools import reduce
from pathlib import Path

import jubilant
import pytest
import requests
import yaml
from lightkube import ApiError, Client, codecs
from lightkube.generic_resource import load_in_cluster_generic_resources
from lightkube.types import CascadeType
from notebook_jobs import (
    RUNTIMECLASS_RESOURCE,
    job_name_for,
    record_result,
    render_notebook_job,
    run_notebook_job,
)
from utils import (
    PODDEFAULT_RESOURCE,
    PROFILE_RESOURCE,
    assert_namespace_active,
    assert_poddefault_created_in_namespace,
    assert_profile_deleted,
    context_from,
    create_poddefault,
)

log = logging.getLogger(__name__)
logging.getLogger("jubilant.wait").setLevel("WARNING")

ASSETS_DIR = Path("assets")
JOB_TEMPLATE_FILE = ASSETS_DIR / "test-job.yaml.j2"
PROFILE_TEMPLATE_FILE = ASSETS_DIR / "test-profile.yaml.j2"
RUNTIMECLASS_TEMPLATE_FILE = ASSETS_DIR / "runtimeclass.yaml.j2"

TESTS_LOCAL_DIR = os.path.abspath(Path("tests"))

NAMESPACE = "test-kubeflow"
JOB_PREFIX = "test-nb"
JOB_RUNTIMECLASS_NAME = "uats"

# Directory (inside the Job pod) where each notebook writes its artifacts, and the host
# directory where the driver stores collected artifacts when --keep-artifacts is set.
POD_ARTIFACTS_DIR = "/tmp/uat-artifacts"
ARTIFACTS_ROOT = Path("artifacts")

PODDEFAULT_WITH_PROXY_PATH = Path("tests") / "proxy-poddefault.yaml.j2"
PODDEFAULT_WITH_TOLERATION_PATH = Path("assets") / "gpu-toleration-poddefault.yaml.j2"
PODDEFAULT_WITH_SECURITY_POLICY_PATH = Path("tests") / "security-policy-poddefault.yaml.j2"

KFP_PODDEFAULT_NAME = "access-ml-pipeline"


def is_local_run() -> bool:
    """Return True if the tests run in local (hostPath) mode."""
    return os.environ.get("LOCAL", "False").strip().lower() in ("true", "1", "yes")


@pytest.fixture(scope="module")
def juju(request: pytest.FixtureRequest):
    """Create a temporary or use an existing Juju model for running tests."""
    keep_models = bool(request.config.getoption("--keep-models"))
    juju_model = request.config.getoption("--model")

    if juju_model:
        model_context = contextlib.nullcontext(jubilant.Juju(model=juju_model))
    else:
        model_context = jubilant.temp_model(keep=keep_models)

    with model_context as juju:
        yield juju

        if request.session.testsfailed:
            log.info("Collecting Juju logs...")
            time.sleep(0.5)  # Wait for Juju to process logs.
            logging_message = juju.debug_log(limit=1000)
            print(logging_message, end="", file=sys.stderr)


@pytest.fixture(scope="module")
def charm_list(request):
    url = request.config.getoption("--bundle")

    if not url:
        return {}

    if url.startswith("http"):
        if not (response := requests.get(url)) or (response.status_code != 200):
            logging.warning(f"Bundle file {url} could not be downloaded")
            return {}

        bundle = yaml.safe_load(response.content.decode("utf-8"))
    else:
        logging.info(f"Using file: {url}")
        if not (filename := re.compile("^file:").sub("", url)) or not Path(filename).exists():
            logging.warning(f"Bundle file {filename} does not exist")
            return {}

        with open(filename, "r") as fid:
            bundle = yaml.safe_load(fid)

    suffixes = ["edge", "beta", "candidate", "stable"]

    return {
        app_name: reduce(
            lambda channel, suffix: channel.removesuffix(suffix), suffixes, charm["channel"]
        )
        for app_name, charm in bundle["applications"].items()
    }


@pytest.fixture(scope="module")
def tests_image(request):
    return request.config.getoption("--test-image")


@pytest.fixture(scope="module")
def k8s_default_runtimeclass_handler(request):
    return request.config.getoption("--k8s-default-runtimeclass-handler")


@pytest.fixture(scope="module")
def include_ambient(request):
    """Retrieve the `--include-ambient-tests` flag from Pytest invocation."""
    return True if request.config.getoption("--include-ambient-tests") else False


@pytest.fixture(scope="module")
def tests_checked_out_commit(request):
    """Retrieve active git commit."""
    head = subprocess.check_output(["git", "rev-parse", "HEAD"])
    return head.decode("UTF-8").rstrip()


@pytest.fixture(scope="module")
def notebook_timeout(request):
    """Return the per-notebook Job timeout (activeDeadlineSeconds) in seconds."""
    return int(request.config.getoption("--notebook-timeout"))


@pytest.fixture(scope="module")
def keep_artifacts(request):
    """Return whether to keep notebook artifacts on the host and Jobs in the cluster."""
    return bool(request.config.getoption("--keep-artifacts"))


@pytest.fixture(scope="module")
def rerun_failed(request):
    """Return how many times a failed notebook should be retried."""
    return int(request.config.getoption("--rerun-failed-notebooks"))


@pytest.fixture(scope="module")
def local_run():
    """Return whether the tests run in local (hostPath) mode."""
    return is_local_run()


@pytest.fixture(scope="module")
def lightkube_client():
    """Initialise Lightkube Client."""
    lightkube_client = Client(trust_env=False)
    load_in_cluster_generic_resources(lightkube_client)
    return lightkube_client


@pytest.fixture(scope="module")
def create_profile(lightkube_client):
    """Create Profile and handle cleanup at the end of the module tests."""
    log.info(f"Creating Profile {NAMESPACE}...")
    resources = list(
        codecs.load_all_yaml(
            PROFILE_TEMPLATE_FILE.read_text(),
            context={"namespace": NAMESPACE},
        )
    )
    assert len(resources) == 1, f"Expected 1 Profile, got {len(resources)}!"
    lightkube_client.create(resources[0])

    yield

    # delete the Profile at the end of the module tests
    log.info(f"Deleting Profile {NAMESPACE}...")
    lightkube_client.delete(PROFILE_RESOURCE, name=NAMESPACE, cascade=CascadeType.FOREGROUND)
    assert_profile_deleted(lightkube_client, NAMESPACE, log)


@pytest.fixture(scope="function")
def create_poddefault_on_proxy(request, lightkube_client):
    """Create PodDefault with proxy env variables for the Notebook inside the Job."""
    # Simply yield if the proxy flag is not set
    if not request.config.getoption("proxy"):
        yield
    else:
        yield from create_poddefault(
            PODDEFAULT_WITH_PROXY_PATH, context_from("proxy", request), NAMESPACE, lightkube_client
        )


@pytest.fixture(scope="function")
def create_poddefault_on_toleration(request, lightkube_client):
    """Create PodDefault with toleration for workload pods created by GPU tests."""
    # Simply yield if the proxy flag is not set
    if not request.config.getoption("toleration"):
        yield
    else:
        yield from create_poddefault(
            PODDEFAULT_WITH_TOLERATION_PATH,
            context_from("toleration", request),
            NAMESPACE,
            lightkube_client,
        )


@pytest.fixture(scope="function")
def create_poddefault_on_security_policy(request, lightkube_client):
    """Create PodDefault with security policy env variables for the Notebook inside the Job."""
    # Simply yield if the option is not set
    if not request.config.getoption("security_policy"):
        yield
    else:
        security_policy_context = {"security_policy": request.config.getoption("security_policy")}
        yield from create_poddefault(
            PODDEFAULT_WITH_SECURITY_POLICY_PATH,
            security_policy_context,
            NAMESPACE,
            lightkube_client,
        )


@pytest.fixture(scope="module")
def istio_mode(include_ambient):
    if include_ambient:
        return "ambient"

    return "sidecar"


@pytest.mark.abort_on_fail
@pytest.mark.dependency()
def test_bundle_correctness(juju, charm_list):
    """Test that the correct bundle is selected.

    Tests are specific to each Charmed Kubeflow version release. This test makes sure that
    the correct version of the bundle, consistent with the tests, is specified. In order to
    check the correctness, we use a YAML bundle that is pulled from the correct URL in the
    bundle-kubeflow repository. This value can be overridden using the `--bundle` argument.
    """

    if not charm_list:
        pytest.skip("charm_list empty. Cannot test bundle correctness")

    status = juju.status()

    # Check that the version is the one expected by this set of tests
    for name, channel_regex in charm_list.items():
        app_channel = status.apps[name].charm_channel
        assert re.compile(channel_regex).match(
            app_channel
        ), f"Failed bundle correctness check for charm {name}. Expected: {channel_regex} Found: {app_channel}"

    # Check that every charm of the bundle is active/idle
    juju.wait(
        lambda status: jubilant.all_active(status, *charm_list)
        and jubilant.all_agents_idle(status, *charm_list),
        error=jubilant.any_error,
        timeout=3600,
        delay=10,
    )


@pytest.mark.dependency()
def test_create_profile(lightkube_client, create_profile):
    """Test Profile creation.

    This test relies on the create_profile fixture, which handles the Profile creation and
    is responsible for cleaning up at the end.
    """
    try:
        profile_created = lightkube_client.get(
            PROFILE_RESOURCE,
            name=NAMESPACE,
        )
    except ApiError as e:
        if e.status == 404:
            profile_created = False
        else:
            raise
    assert profile_created, f"Profile {NAMESPACE} not found!"

    assert_namespace_active(lightkube_client, NAMESPACE)

    # Wait until KFP PodDefault is created in the namespace
    assert_poddefault_created_in_namespace(lightkube_client, KFP_PODDEFAULT_NAME, NAMESPACE)

    # Sync of other PodDefaults to the namespace can take up to 10 seconds
    # Wait here is necessary to allow the creation of PodDefaults before Job is created
    sleep_time_seconds = 10
    log.info(
        f"Sleeping for {sleep_time_seconds}s to allow the creation of PodDefaults in {NAMESPACE} namespace.."
    )
    time.sleep(sleep_time_seconds)

    # Get PodDefaults in the test namespace
    created_poddefaults_list = lightkube_client.list(PODDEFAULT_RESOURCE, namespace=NAMESPACE)
    created_poddefaults_names = [pd.metadata.name for pd in created_poddefaults_list]

    # Print the names of PodDefaults in the test namespace
    log.info(f"PodDefaults in {NAMESPACE} namespace are {created_poddefaults_names}.")


@pytest.fixture(scope="module")
def runtimeclass(local_run, k8s_default_runtimeclass_handler, lightkube_client):
    """Create the RuntimeClass used for PSS exemption in local runs; clean up after."""
    if not local_run:
        yield
        return

    log.info("Creating the RuntimeClass for exemption from Pod Security Standards...")
    resources = list(
        codecs.load_all_yaml(
            RUNTIMECLASS_TEMPLATE_FILE.read_text(),
            context={
                "runtimeclass_handler": k8s_default_runtimeclass_handler,
                "runtimeclass_name": JOB_RUNTIMECLASS_NAME,
            },
        )
    )
    assert len(resources) == 1, f"Expected 1 RuntimeClass, got {len(resources)}!"
    lightkube_client.create(resources[0])

    yield

    log.info("Deleting the RuntimeClass for the Job...")
    try:
        lightkube_client.delete(RUNTIMECLASS_RESOURCE, name=JOB_RUNTIMECLASS_NAME)
    except ApiError as error:
        if error.status.code != 404:
            raise


def _in_pod_notebook_path(host_path: str, local_run: bool) -> str:
    """Map a host notebook path to its path inside the Job pod."""
    relative = os.path.relpath(host_path, start=TESTS_LOCAL_DIR)
    if local_run:
        return f"/tests/{relative}"
    return f"/tests/charmed-kubeflow-uats/tests/{relative}"


def _notebook_job_context(
    job_name,
    notebook_path,
    local_run,
    tests_image,
    tests_remote_commit,
    notebook_timeout,
    keep_artifacts,
    proxy,
    security_policy,
    kubeflow_namespace,
    istio_mode,
):
    """Build the Jinja context for rendering a single-notebook Job."""
    return {
        "job_name": job_name,
        "tests_local_run": local_run,
        "tests_local_dir": TESTS_LOCAL_DIR,
        "tests_image": tests_image,
        "tests_remote_commit": tests_remote_commit,
        "notebook_path": _in_pod_notebook_path(notebook_path, local_run),
        "artifacts_dir": POD_ARTIFACTS_DIR,
        "notebook_timeout": notebook_timeout,
        "keep_artifacts": keep_artifacts,
        "proxy": proxy,
        "security_policy": security_policy,
        "kubeflow_namespace": kubeflow_namespace,
        "user_namespace": NAMESPACE,
        "istio_mode": istio_mode,
    }


def _failure_message(result) -> str:
    """Build a concise, actionable failure message for a notebook result."""
    lines = [f"Notebook '{result.name}' {result.status}."]
    if result.failing_cell is not None:
        lines.append(f"Failing cell: {result.failing_cell}")
    if result.error_summary:
        lines.append(f"Error: {result.error_summary}")
    if result.artifacts_dir:
        lines.append(f"Artifacts: {result.artifacts_dir}")
    if result.log_tail:
        lines.append(f"Recent logs:\n{result.log_tail}")
    return "\n".join(lines)


@pytest.mark.dependency(depends=["test_create_profile"])
def test_notebook_workload(
    notebook,
    juju,
    lightkube_client,
    tests_image,
    tests_checked_out_commit,
    istio_mode,
    local_run,
    notebook_timeout,
    keep_artifacts,
    rerun_failed,
    runtimeclass,
    create_poddefault_on_proxy,
    create_poddefault_on_toleration,
    create_poddefault_on_security_policy,
    request,
):
    """Run a single UAT notebook as an isolated Kubernetes Job.

    Each notebook runs in its own Job (portable, no shared storage). The notebook's
    outcome is recovered from its logs, a per-notebook summary is recorded, and the Job
    is retried up to ``--rerun-failed-notebooks`` times before being reported as failed.
    """
    notebook_name = Path(notebook).stem
    log.info(f"Istio Mode: {istio_mode}")

    result = None
    for attempt in range(1 + rerun_failed):
        job_name = job_name_for(JOB_PREFIX, notebook_name)
        if attempt:
            job_name = f"{job_name}-retry{attempt}"[:63]
        context = _notebook_job_context(
            job_name=job_name,
            notebook_path=notebook,
            local_run=local_run,
            tests_image=tests_image,
            tests_remote_commit=tests_checked_out_commit,
            notebook_timeout=notebook_timeout,
            keep_artifacts=keep_artifacts,
            proxy=bool(request.config.getoption("proxy")),
            security_policy=request.config.getoption("security_policy") != "privileged",
            kubeflow_namespace=juju.model,
            istio_mode=istio_mode,
        )
        manifest = render_notebook_job(str(JOB_TEMPLATE_FILE), context)
        result = run_notebook_job(
            client=lightkube_client,
            notebook_name=notebook_name,
            manifest=manifest,
            namespace=NAMESPACE,
            timeout=notebook_timeout,
            keep_artifacts=keep_artifacts,
            artifacts_root=str(ARTIFACTS_ROOT),
        )
        record_result(request.config, result)
        if result.succeeded:
            break
        if attempt < rerun_failed:
            log.warning(f"Notebook '{notebook_name}' {result.status}; retrying ({attempt + 1})...")

    assert result.succeeded, _failure_message(result)
