# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
import logging
from pathlib import Path

import pytest
from lightkube import ApiError, Client, codecs
from lightkube.generic_resource import load_in_cluster_generic_resources
from lightkube.models.core_v1 import Container, PodSpec
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.core_v1 import Pod
from lightkube.types import CascadeType
from utils import (
    PROFILE_RESOURCE,
    assert_namespace_active,
    assert_pod_running,
    assert_profile_deleted,
    assert_service_account_exists,
    exec_in_pod,
)

log = logging.getLogger(__name__)

# Assets directory is relative to the repository root
ASSETS_DIR = Path(__file__).parent.parent.parent / "assets"
AMBIENT_PROFILE_TEMPLATE_FILE = ASSETS_DIR / "ambient-profile.yaml.j2"

NAMESPACE_1 = "profile1"
NAMESPACE_2 = "profile2"
CURL_POD_NAME = "ambient-test-curl"


def pytest_collection_modifyitems(config, items):
    """Skip ambient tests unless --include-ambient-tests is passed."""
    if not config.getoption("--include-ambient-tests", default=False):
        skip_ambient = pytest.mark.skip(reason="need --include-ambient-tests option to run")
        for item in items:
            if "ambient" in item.nodeid:
                item.add_marker(skip_ambient)


@pytest.fixture(scope="module")
def lightkube_client():
    """Initialise Lightkube Client."""
    lightkube_client = Client(trust_env=False)
    load_in_cluster_generic_resources(lightkube_client)
    return lightkube_client


def _create_and_cleanup_profile(client: Client, namespace: str):
    """Helper to create a profile and handle cleanup. Use in fixtures with yield."""
    log.info(f"Creating Profile {namespace}...")
    profile = list(
        codecs.load_all_yaml(
            AMBIENT_PROFILE_TEMPLATE_FILE.read_text(),
            context={"namespace": namespace},
        )
    )[0]

    client.create(profile)

    # Wait for namespace to become active
    assert_namespace_active(client, namespace)

    # Wait for the default-editor service account to be created by the profile controller
    assert_service_account_exists(client, "default-editor", namespace)

    yield

    # Delete the Profile at the end
    log.info(f"Deleting Profile {namespace}...")
    try:
        client.delete(PROFILE_RESOURCE, name=namespace, cascade=CascadeType.FOREGROUND)
        assert_profile_deleted(client, namespace, log)
    except ApiError as e:
        if e.status.code != 404:
            raise
        log.info(f"Profile {namespace} already deleted")


@pytest.fixture(scope="module")
def create_profile_1(lightkube_client):
    """Create Profile 1 (profile1) and handle cleanup at the end."""
    yield from _create_and_cleanup_profile(lightkube_client, NAMESPACE_1)


@pytest.fixture(scope="module")
def create_profile_2(lightkube_client):
    """Create Profile 2 (profile2) and handle cleanup at the end."""
    yield from _create_and_cleanup_profile(lightkube_client, NAMESPACE_2)


@pytest.fixture(scope="module")
def create_curl_pod(lightkube_client, create_profile_2):
    """Create a curl pod in profile 2."""
    log.info(f"Creating curl pod {NAMESPACE_2}/{CURL_POD_NAME}...")

    # Create pod directly using lightkube API
    pod = Pod(
        metadata=ObjectMeta(
            name=CURL_POD_NAME,
            namespace=NAMESPACE_2,
        ),
        spec=PodSpec(
            serviceAccountName="default-editor",
            restartPolicy="Never",
            containers=[
                Container(
                    name="curl",
                    image="curlimages/curl:latest",
                    command=["sleep", "infinity"],
                )
            ],
        ),
    )

    lightkube_client.create(pod, namespace=NAMESPACE_2)

    # Wait for pod to be running
    assert_pod_running(lightkube_client, CURL_POD_NAME, NAMESPACE_2)

    yield CURL_POD_NAME

    # Cleanup
    log.info(f"Deleting pod {NAMESPACE_2}/{CURL_POD_NAME}...")
    try:
        lightkube_client.delete(Pod, name=CURL_POD_NAME, namespace=NAMESPACE_2)
    except ApiError as e:
        if e.status.code != 404:
            raise
        log.info(f"Pod {CURL_POD_NAME} already deleted")


@pytest.mark.run(order=2)
@pytest.mark.dependency(
    depends=["driver/test_kubeflow_workloads.py::test_bundle_correctness"], scope="session"
)
def test_ambient_rbac_isolation(
    lightkube_client, create_profile_1, create_profile_2, create_curl_pod
):
    """Test that ambient mesh prevents cross-profile access via RBAC.

    This test verifies that a pod in profile2 cannot access resources
    in profile1 namespace via the KFP API, even when impersonating the correct user.
    This demonstrates that ambient mesh is preventing header spoofing attacks.
    """
    pod_name = create_curl_pod

    # The curl command to test RBAC isolation
    # This attempts to list experiments in profile1 namespace
    # while impersonating profile1@email.com (the CORRECT user)
    curl_command = [
        "curl",
        "-s",
        "-w",
        "\\nHTTP_CODE:%{http_code}",
        "-H",
        f"kubeflow-userid: {NAMESPACE_1}@email.com",
        f"ml-pipeline.kubeflow.svc:8888/apis/v2beta1/experiments?namespace={NAMESPACE_1}",
    ]

    stdout, stderr, returncode = exec_in_pod(pod_name, NAMESPACE_2, curl_command)

    log.info(f"Command output:\n{stdout}")
    if stderr:
        log.info(f"Command stderr:\n{stderr}")

    # Extract HTTP code from output
    http_code = None
    if "HTTP_CODE:" in stdout:
        http_code_str = stdout.split("HTTP_CODE:")[-1].strip()
        try:
            http_code = int(http_code_str)
        except ValueError:
            log.error(f"Could not parse HTTP code from: {http_code_str}")

    # Verify we got a 403 (Forbidden) response
    assert http_code == 403, (
        f"Expected HTTP 403 (RBAC: access denied), but got {http_code}. "
        f"This suggests ambient mesh RBAC isolation is not working correctly. "
        f"Output: {stdout}"
    )

    # Verify the response contains RBAC denial message
    assert (
        "RBAC: access denied" in stdout or "access denied" in stdout.lower()
    ), f"Expected 'RBAC: access denied' in response, but got: {stdout}"

    log.info("✓ Ambient RBAC isolation test passed: Cross-profile access was correctly denied!")
