# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Dict

import pytest
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
    fetch_job_logs,
    wait_for_job,
)

log = logging.getLogger(__name__)

ASSETS_DIR = Path("assets")
JOB_TEMPLATE_FILE = ASSETS_DIR / "test-job.yaml.j2"
PROFILE_TEMPLATE_FILE = ASSETS_DIR / "test-profile.yaml.j2"

TESTS_LOCAL_RUN = eval(os.environ.get("LOCAL"))
TESTS_LOCAL_DIR = os.path.abspath(Path("tests"))

TESTS_IMAGE_CPU = "kubeflownotebookswg/jupyter-scipy:v1.9.0"
TESTS_IMAGE_GPU = "kubeflownotebookswg/jupyter-tensorflow-cuda-full:v1.9.0"

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

KFP_PODDEFAULT_NAME = "access-ml-pipeline"


@pytest.fixture(scope="session")
def pytest_filter(request):
    """Retrieve filter from Pytest invocation."""
    filter = request.config.getoption("filter")
    return f"-k '{filter}'" if filter else ""

@pytest.fixture(scope="session")
def use_gpu_image(request):
    """Retrieve use-gpu-image from Pytest invocation."""
    print(request.config.getoption("--use-gpu-image"))
    return True if request.config.getoption("--use-gpu-image") else False

@pytest.fixture(scope="session")
def tests_checked_out_commit(request):
    """Retrieve active git commit."""
    head = subprocess.check_output(["git", "rev-parse", "HEAD"])
    return head.decode("UTF-8").rstrip()


@pytest.fixture(scope="session")
def pytest_cmd(pytest_filter):
    """Format the Pytest command."""
    return f"{PYTEST_CMD_BASE} {pytest_filter}" if pytest_filter else PYTEST_CMD_BASE


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
def create_poddefaults_on_proxy(request, lightkube_client):
    """Create PodDefault with proxy env variables for the Notebook inside the Job."""
    # Simply yield if the proxy flag is not set
    if not request.config.getoption("proxy"):
        yield
    else:
        log.info("Adding PodDefault with proxy settings.")
        poddefault_resource = codecs.load_all_yaml(
            PODDEFAULT_WITH_PROXY_PATH.read_text(),
            context=proxy_context(request),
        )
        # Using the first item of the list of poddefault_resource. It is a one item list.
        lightkube_client.create(poddefault_resource[0], namespace=NAMESPACE)

        yield

        # delete the PodDefault at the end of the module tests
        log.info("Deleting PodDefault...")
        poddefault_resource = codecs.load_all_yaml(
            PODDEFAULT_WITH_PROXY_PATH.read_text(),
            context=proxy_context(request),
        )
        poddefault_name = poddefault_resource[0].metadata.name
        lightkube_client.delete(PODDEFAULT_RESOURCE, name=poddefault_name, namespace=NAMESPACE)


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
    create_poddefaults_on_proxy,
    use_gpu_image,
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
                "tests_image": TESTS_IMAGE_GPU if use_gpu_image else TESTS_IMAGE_CPU,
                "tests_remote_commit": tests_checked_out_commit,
                "pytest_cmd": pytest_cmd,
                "proxy": True if request.config.getoption("proxy") else False,
                "use_gpu_image": use_gpu_image,
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


def proxy_context(request) -> Dict[str, str]:
    """Return a dictionary with proxy environment variables from user input."""
    proxy_context = {}
    for proxy in request.config.getoption("proxy"):
        key, value = proxy.split("=")
        proxy_context[key] = value
    return proxy_context
