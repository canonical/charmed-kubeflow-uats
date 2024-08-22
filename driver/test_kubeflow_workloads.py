# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import os
import subprocess
from pathlib import Path
from typing import Dict

import pytest
from lightkube import ApiError, Client, codecs
from lightkube.generic_resource import (
    create_global_resource,
    create_namespaced_resource,
    load_in_cluster_generic_resources,
)
from utils import assert_namespace_active, delete_job, fetch_job_logs, wait_for_job

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


@pytest.fixture(scope="session")
def pytest_filter(request):
    """Retrieve filter from Pytest invocation."""
    filter = request.config.getoption("filter")
    return f"-k '{filter}'" if filter else ""


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
    lightkube_client.delete(PROFILE_RESOURCE, name=NAMESPACE)


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
        log.info(f"Deleting PodDefault {PODDEFAULT_WITH_PROXY_NAME}...")
        lightkube_client.delete(
            PODDEFAULT_RESOURCE, namespace=NAMESPACE
        )


@pytest.mark.abort_on_fail
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


def test_kubeflow_workloads(
    lightkube_client, pytest_cmd, tests_checked_out_commit, request, create_poddefaults_on_proxy
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


def teardown_module():
    """Cleanup resources."""
    log.info(f"Deleting Job {NAMESPACE}/{JOB_NAME}...")
    delete_job(JOB_NAME, NAMESPACE)


def proxy_context(request) -> Dict[str, str]:
    """Return a dictionary with proxy environment variables from user input."""
    proxy_context = {}
    for proxy in request.config.getoption("proxy"):
        key, value = proxy.split("=")
        proxy_context[key] = value
    return proxy_context
