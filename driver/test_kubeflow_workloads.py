# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import os
import re
import subprocess
import time
from pathlib import Path

import pytest
import requests
import yaml
from lightkube import ApiError, Client, codecs
from lightkube.generic_resource import (
    create_global_resource,
    create_namespaced_resource,
    load_in_cluster_generic_resources,
)
from lightkube.types import CascadeType
from utils import (
    assert_namespace_active,
    assert_poddefault_created_in_namespace,
    assert_profile_deleted,
    context_from,
    create_poddefault,
    fetch_job_logs,
    wait_for_job,
)

log = logging.getLogger(__name__)

ASSETS_DIR = Path("assets")
JOB_TEMPLATE_FILE = ASSETS_DIR / "test-job.yaml.j2"
PROFILE_TEMPLATE_FILE = ASSETS_DIR / "test-profile.yaml.j2"

TESTS_LOCAL_RUN = eval(os.environ.get("LOCAL"))
TESTS_LOCAL_DIR = os.path.abspath(Path("tests"))

TESTS_IMAGE = "kubeflownotebookswg/jupyter-scipy:v1.9.0"

NAMESPACE = "test-kubeflow"
PROFILE_RESOURCE = create_global_resource(
    group="kubeflow.org",
    version="v1",
    kind="profile",
    plural="profiles",
)

JOB_NAME = "test-kubeflow"

PYTEST_CMD_BASE = "pytest"

PODDEFAULT_RESOURCE = create_namespaced_resource(
    group="kubeflow.org",
    version="v1alpha1",
    kind="poddefault",
    plural="poddefaults",
)
PODDEFAULT_WITH_PROXY_PATH = Path("tests") / "proxy-poddefault.yaml.j2"
PODDEFAULT_WITH_TOLERATION_PATH = Path("assets") / "gpu-toleration-poddefault.yaml.j2"

KFP_PODDEFAULT_NAME = "access-ml-pipeline"


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
        if not (filename := re.compile("^file:").sub("", url)) or not Path(filename).exists():
            logging.warning(f"Bundle file {filename} does not exist")
            return {}

        with open(filename, "r") as fid:
            bundle = yaml.safe_load(fid)

    return {
        app_name: charm["channel"].split("/")[0] + "/*"
        for app_name, charm in bundle["applications"].items()
    }


@pytest.fixture(scope="module")
def pytest_filter(request):
    """Retrieve filter from Pytest invocation."""
    filter = request.config.getoption("filter")
    return f"-k '{filter}'" if filter else ""


@pytest.fixture(scope="module")
def include_gpu_tests(request):
    """Retrieve the `--include-gpu-tests` flag from Pytest invocation."""
    return True if request.config.getoption("--include-gpu-tests") else False


@pytest.fixture(scope="module")
def kubeflow_model(request, ops_test):
    """Retrieve name of the model where Kubeflow is deployed."""
    model_name = request.config.getoption("--kubeflow-model")
    return model_name if model_name else ops_test.model.name


@pytest.fixture(scope="module")
def tests_checked_out_commit(request):
    """Retrieve active git commit."""
    head = subprocess.check_output(["git", "rev-parse", "HEAD"])
    return head.decode("UTF-8").rstrip()


@pytest.fixture(scope="module")
def pytest_cmd(pytest_filter, include_gpu_tests):
    """Format the Pytest command."""
    cmd = PYTEST_CMD_BASE
    if pytest_filter:
        cmd += f" {pytest_filter}"
    if include_gpu_tests:
        cmd += " --include-gpu-tests"
    return cmd


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


@pytest.mark.abort_on_fail
async def test_bundle_correctness(ops_test, kubeflow_model, charm_list):
    """Test that the correct bundle is selected.

    Tests are specific to each Charmed Kubeflow version release. This test makes sure that
    the correct version of the bundle, consistent with the tests, is specified. In order to
    check the correctness, we use a YAML bundle that is pulled from the correct URL in the
    bundle-kubeflow repository. This value can be overridden using the `--bundle` argument. 
    """

    if not charm_list:
        pytest.skip("charm_list empty. Cannot test bundle correctness")

    model = await ops_test.track_model("kubeflow", model_name=kubeflow_model, use_existing=True)
    status = await model.get_status()

    # Check that the version is the one expected by this set of tests
    for name, channel_regex in charm_list.items():
        app_channel = status["applications"][name]["charm-channel"]
        assert re.compile(channel_regex).match(
            app_channel
        ), f"Failed bundle correctness check. Expected: {channel_regex} Found: {app_channel}"

    # Check that everything is active/idle
    await ops_test.model.wait_for_idle(
        apps=list(status["applications"]),
        timeout=3600,
        idle_period=30,
        status="active",
        raise_on_error=True,
    )


@pytest.mark.dependency()
async def test_create_profile(lightkube_client, create_profile):
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


@pytest.mark.dependency(depends=["test_create_profile"])
def test_kubeflow_workloads(
    lightkube_client,
    pytest_cmd,
    tests_checked_out_commit,
    request,
    create_poddefault_on_proxy,
    create_poddefault_on_toleration,
):
    """Run a K8s Job to execute the notebook tests."""
    log.info(f"Starting Kubernetes Job {NAMESPACE}/{JOB_NAME} to run notebook tests...")
    resources = list(
        codecs.load_all_yaml(
            JOB_TEMPLATE_FILE.read_text(),
            context={
                "job_name": JOB_NAME,
                "tests_local_run": TESTS_LOCAL_RUN,
                "tests_local_dir": TESTS_LOCAL_DIR,
                "tests_image": TESTS_IMAGE,
                "tests_remote_commit": tests_checked_out_commit,
                "pytest_cmd": pytest_cmd,
                "proxy": True if request.config.getoption("proxy") else False,
            },
        )
    )

    assert len(resources) == 1, f"Expected 1 Job, got {len(resources)}!"
    lightkube_client.create(resources[0], namespace=NAMESPACE)

    try:
        wait_for_job(lightkube_client, JOB_NAME, NAMESPACE)
    except ValueError:
        pytest.fail(
            f"Something went wrong while running Job {NAMESPACE}/{JOB_NAME}. Please inspect the"
            " attached logs for more info..."
        )
    finally:
        log.info("Fetching Job logs...")
        fetch_job_logs(JOB_NAME, NAMESPACE, TESTS_LOCAL_RUN)
