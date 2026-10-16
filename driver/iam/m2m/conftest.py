# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Conftest for M2M identity integration tests."""

import logging
import sys
from pathlib import Path

import pytest
from lightkube import Client

# Add the driver directory to path so shared modules (utils, ingress) are importable.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from iam.ui.helpers import (  # noqa: E402  (import must follow the sys.path setup above)
    authorize_contributor,
    create_oauth_client,
    delete_oauth_client,
    get_jwt_issuer_url,
    get_token,
)
from ingress import find_gateway_for_domain, gateway_service_name, get_service_lb_ip  # noqa: E402

log = logging.getLogger(__name__)

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
def issuer_url(application_model: str) -> str:
    """JWT issuer URL trusted by the gateway's RequestAuthentication."""
    return get_jwt_issuer_url(application_model)@pytest.fixture()


def authorized_client(
    lightkube_client: Client,
    create_profile,
    gateway_principals,
    keep_artifacts: bool,
    is_mlflow_standalone: bool,
):
    """Create an OAuth client and authorize it to access the application.
    
    For Kubeflow, the client is added as a contributor to the test Profile.
    
    For MLflow standalone, the client is granted access to the test workspace.
    """
    client_name = (
        f"uat-m2m-authorized-{'mlflow' if is_mlflow_standalone else 'kubeflow'}"
    )
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
    client_name = (
        f"uat-m2m-unauthorized-{'mlflow' if is_mlflow_standalone else 'kubeflow'}"
    )
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
