# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""M2M (machine-to-machine) identity integration tests for standalone MLflow.

These tests validate that a token issued by the Identity Platform (Hydra) can be used to
reach the MLflow tracking server's REST API through the M2M istio ingress gateway from
*outside* the cluster, and that unauthenticated or unauthorized requests are rejected.

They assume a standalone MLflow + Identity Platform deployment with an ambient service mesh
(the ``mlflow-ambient-iam`` setup in charmed-kubeflow-solutions): Juju models ``iam`` and
``mlflow``, an istio ingress gateway serving ``api.mlflow.com``, Hydra, oauth2-proxy, and the
request-authentication-configurator mapping the JWT identity to the ``mlflow-userid`` header.

The authorized client is granted admin on the test workspace through a throwaway
``data-integrator`` over the ``mlflow-client`` relation — the same sanctioned path a real
programmatic consumer uses — created and removed by this suite, mutating no existing config.
"""

import json
import logging
from uuid import uuid4

import jubilant
import pytest

from helpers import (  # isort:skip
    mlflow_create_experiment_with_jwt_warmup,
    mlflow_delete_experiment,
    mlflow_experiments_search,
)

log = logging.getLogger(__name__)

MLFLOW_DEFAULT_DOMAIN_M2M = "api.mlflow.com"
# The mlflow-server application and the throwaway data-integrator used to grant the test
# client; the grant app name is distinct so it never clashes with a deployment's own grants.
MLFLOW_SERVER_APP = "mlflow-server"
GRANT_APP = "uat-m2m-grant"


@pytest.fixture(scope="module")
def m2m_domain(request: pytest.FixtureRequest) -> str:
    """Application M2M hostname."""
    return (
        domain
        if (domain := request.config.getoption("--iam-domain-m2m"))
        else MLFLOW_DEFAULT_DOMAIN_M2M
    )


@pytest.fixture(scope="module")
def mlflow_workspace(request: pytest.FixtureRequest) -> str:
    """The MLflow workspace the M2M tests target."""
    return request.config.getoption("--mlflow-workspace")


@pytest.fixture(scope="module")
def mlflow_juju(application_model: str) -> jubilant.Juju:
    """A Jubilant handle to the MLflow model."""
    return jubilant.Juju(model=application_model)


@pytest.fixture()
def mlflow_client_grant(
    authorized_client, mlflow_workspace: str, mlflow_juju: jubilant.Juju, keep_artifacts: bool
):
    """Grant the authorized client admin on the test workspace via a throwaway data-integrator.

    Deploys a ``data-integrator`` configured with the client id as the MLflow username and an
    admin grant on the workspace, relates it over ``mlflow-client`` (the sanctioned grant path),
    waits for the grant to be provisioned, and removes it on teardown. No existing charm config
    is mutated.
    """
    client_id, _ = authorized_client
    permissions = json.dumps(
        [
            {
                "resource_type": "workspace",
                "resource_name": mlflow_workspace,
                "privileges": ["admin"],
            }
        ]
    )
    log.info(f"Granting MLflow user '{client_id}' admin on workspace '{mlflow_workspace}'...")
    mlflow_juju.deploy(
        "data-integrator",
        app=GRANT_APP,
        channel="latest/edge",
        config={"entity-name": client_id, "entity-permissions": permissions},
    )
    mlflow_juju.integrate(f"{GRANT_APP}:mlflow", f"{MLFLOW_SERVER_APP}:mlflow-client")
    mlflow_juju.wait(
        lambda status: jubilant.all_active(status, GRANT_APP, MLFLOW_SERVER_APP),
        timeout=600,
    )

    yield client_id

    if keep_artifacts:
        log.info(f"Keeping data-integrator {GRANT_APP} (--keep-artifacts set)")
        return
    mlflow_juju.remove_application(GRANT_APP, destroy_storage=True)
    mlflow_juju.wait(lambda status: GRANT_APP not in status.apps)


def test_authorized_token_reaches_mlflow(
    authorized_token: str,
    mlflow_client_grant,
    m2m_domain: str,
    gateway_ip: str,
    mlflow_workspace: str,
):
    """A valid token from a granted client can create an experiment in the workspace.

    Confirms the full path: Hydra issues the token, the gateway's RequestAuthentication
    validates the issuer, the request-authentication-configurator maps the identity to the
    ``mlflow-userid`` header, and MLflow authorizes the workspace-scoped write. A write
    (``experiments/create``) is used rather than ``experiments/search`` because search returns
    HTTP 200 to any authenticated user and so cannot distinguish a granted client from an
    ungranted one (see ``test_unauthorized_token_is_forbidden``).
    """
    name = f"uat-m2m-{uuid4().hex[:8]}"
    http_code, body = mlflow_create_experiment_with_jwt_warmup(
        m2m_domain, gateway_ip, authorized_token, mlflow_workspace, name
    )

    assert http_code == 200, f"Expected HTTP 200, got {http_code}. Body: {body}"
    log.info("✓ Authorized token successfully created an MLflow experiment.")

    # Best-effort cleanup of the throwaway experiment; never fail the test on a cleanup error.
    try:
        experiment_id = json.loads(body).get("experiment_id")
        if experiment_id:
            mlflow_delete_experiment(
                m2m_domain, gateway_ip, authorized_token, mlflow_workspace, experiment_id
            )
    except Exception as exc:
        log.warning(f"Failed to clean up test experiment: {exc}")


def test_missing_token_is_rejected(m2m_domain: str, gateway_ip: str, mlflow_workspace: str):
    """A request without a token carries no identity and is denied 401."""
    http_code, body = mlflow_experiments_search(m2m_domain, gateway_ip, None, mlflow_workspace)

    assert (
        http_code == 403
    ), f"Expected HTTP 403 for a request without a token, got {http_code}. Body: {body}"
    log.info("✓ Request without a token was correctly denied.")


def test_invalid_token_is_rejected(m2m_domain: str, gateway_ip: str, mlflow_workspace: str):
    """A request with an invalid token is rejected by the gateway's RequestAuthentication."""
    http_code, body = mlflow_experiments_search(
        m2m_domain, gateway_ip, "not-a-valid-jwt", mlflow_workspace
    )

    assert (
        http_code == 401
    ), f"Expected HTTP 401 for an invalid token, got {http_code}. Body: {body}"
    log.info("✓ Request with an invalid token was correctly rejected.")


def test_unauthorized_token_is_forbidden(
    unauthorized_token: str, m2m_domain: str, gateway_ip: str, mlflow_workspace: str
):
    """A valid token from a client with no workspace grant is forbidden by MLflow RBAC.

    ``experiments/search`` returns HTTP 200 (an empty list) to any authenticated user, so it
    cannot show that the ungranted client is denied. An RBAC-gated write (``experiments/create``)
    is used instead: the grant is what the create needs, so without it MLflow returns HTTP 403
    (and, being denied, the create leaves no experiment to clean up).
    """
    name = f"uat-m2m-{uuid4().hex[:8]}"
    http_code, body = mlflow_create_experiment_with_jwt_warmup(
        m2m_domain, gateway_ip, unauthorized_token, mlflow_workspace, name
    )

    assert (
        http_code == 403
    ), f"Expected HTTP 403 (RBAC denied) for an ungranted client, got {http_code}. Body: {body}"
    log.info("✓ Valid token from an ungranted client was correctly forbidden.")
