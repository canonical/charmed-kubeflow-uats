# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Conftest for M2M identity integration tests."""

import logging
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from lightkube import Client, codecs

# Add the driver directory to path so shared modules (utils, ingress) are importable.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from iam.m2m.helpers import (  # noqa: E402  (import must follow the sys.path setup above)
    authorize_contributor,
    create_oauth_client,
    delete_oauth_client,
    get_jwt_issuer_url,
    get_token,
)
from ingress import find_gateway_for_domain, gateway_service_name, get_service_lb_ip  # noqa: E402
from utils import (  # noqa: E402
    PROFILE_RESOURCE,
    assert_namespace_active,
    assert_resource_deleted,
)

log = logging.getLogger(__name__)

# Assets directory is relative to the repository root.
ASSETS_DIR = Path(__file__).parent.parent.parent.parent / "assets"

PROFILE_NAME = "test-m2m"
PROFILE_TEMPLATE_FILE = ASSETS_DIR / "test-profile.yaml.j2"

IAM_MODEL = "iam"


@pytest.fixture(scope="module")
def m2m_gateway(lightkube_client: Client, application_model: str, m2m_domain: str) -> str:
    """Name of the istio Gateway serving the M2M domain.

    Discovered dynamically by matching the listener hostname, so the charm/app name
    does not need to be hardcoded.
    """
    return find_gateway_for_domain(lightkube_client, application_model, m2m_domain)


@pytest.fixture(scope="module")
def gateway_ip(lightkube_client: Client, m2m_gateway: str, application_model: str) -> str:
    """LoadBalancer IP of the M2M ingress gateway."""
    return get_service_lb_ip(
        lightkube_client, application_model, gateway_service_name(m2m_gateway)
    )


@pytest.fixture(scope="module")
def gateway_principals(m2m_gateway: str, application_model: str) -> list[str]:
    """Istio principal of the M2M ingress gateway serving KServe."""
    return [f"cluster.local/ns/{application_model}/sa/{gateway_service_name(m2m_gateway)}"]


@pytest.fixture(scope="module")
def issuer_url(application_model: str) -> str:
    """JWT issuer URL trusted by the gateway's RequestAuthentication."""
    return get_jwt_issuer_url(application_model)


@pytest.fixture(scope="module")
def create_profile(lightkube_client: Client, keep_artifacts: bool):
    """Create the test Profile and clean it up at the end of the module."""
    profile_uuid = str(uuid4()).split("-")[0]  # get only the first portion of uuid
    profile_name = namespace_name = f"{PROFILE_NAME}-{profile_uuid}"
    log.info(f"Creating Profile {profile_name}...")
    resources = list(
        codecs.load_all_yaml(
            PROFILE_TEMPLATE_FILE.read_text(),
            context={"namespace": namespace_name},
        )
    )
    assert len(resources) == 1, f"Expected 1 Profile, got {len(resources)}!"
    lightkube_client.create(resources[0])

    assert_namespace_active(lightkube_client, namespace_name)

    yield profile_name, profile_uuid

    if keep_artifacts:
        log.info(f"Keeping Profile {profile_name} (--keep-artifacts set)")
        return

    assert_resource_deleted(lightkube_client, PROFILE_RESOURCE, profile_name)


@pytest.fixture()
def authorized_client(
    lightkube_client: Client,
    gateway_principals,
    keep_artifacts: bool,
    is_mlflow_standalone: bool,
):
    """Create an OAuth client and authorize it to access the application.

    For Kubeflow, the client is added as a contributor to the test Profile.

    For MLflow standalone, the client is granted access to the test workspace.
    """
    client_name = f"uat-m2m-authorized-{'mlflow' if is_mlflow_standalone else 'kubeflow'}"
    client_id, client_secret = create_oauth_client(IAM_MODEL, client_name)

    if not is_mlflow_standalone:
        profile_name, _ = create_profile
        authorize_contributor(
            lightkube_client,
            namespace=profile_name,
            user=client_id,
            role="edit",
            principals=gateway_principals,
        )

    yield client_id, client_secret

    if keep_artifacts:
        log.info(f"Keeping OAuth client {client_id} (--keep-artifacts set)")
        return
    delete_oauth_client(IAM_MODEL, client_id)


@pytest.fixture(scope="module")
def unauthorized_client(keep_artifacts: bool, is_mlflow_standalone: bool):
    """Create an OAuth client that is not authorized to access the application.

    For Kubeflow, the client is not added as a contributor to any Profile.

    For MLflow standalone, the client is not granted access to any workspace.
    """
    client_name = f"uat-m2m-unauthorized-{'mlflow' if is_mlflow_standalone else 'kubeflow'}"
    client_id, client_secret = create_oauth_client(IAM_MODEL, client_name)

    yield client_id, client_secret

    if keep_artifacts:
        return
    delete_oauth_client(IAM_MODEL, client_id)


@pytest.fixture()
def authorized_token(authorized_client, issuer_url: str) -> str:
    """A valid access token for the authorized OAuth client."""
    client_id, client_secret = authorized_client
    return get_token(client_id, client_secret, issuer_url)


@pytest.fixture(scope="module")
def unauthorized_token(unauthorized_client, issuer_url: str) -> str:
    """A valid access token for the unauthorized OAuth client."""
    client_id, client_secret = unauthorized_client
    return get_token(client_id, client_secret, issuer_url)
