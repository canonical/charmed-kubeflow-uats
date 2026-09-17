# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""M2M (machine-to-machine) identity integration tests.

These tests validate that a token issued by the Identity Platform (Hydra) can be
used to reach a KServe InferenceService through the Istio ingress gateway from
*outside* the cluster, and that unauthenticated or unauthorized requests are
rejected.

They assume a Kubeflow + Identity Platform deployment with an ambient service mesh
(e.g. the ``kubeflow-ambient-iam`` setup in charmed-kubeflow-solutions): Juju models
``iam`` and ``kubeflow``, an Istio ingress gateway serving ``api.kubeflow.com``,
Hydra, and oauth2-proxy.
"""

import logging
from pathlib import Path
from uuid import uuid4

import jubilant
import pytest
from helpers import (
    INFERENCE_SERVICE_RESOURCE,
    authorize_contributor,
    create_oauth_client,
    delete_oauth_client,
    get_jwt_issuer_url,
    get_token,
    patch_gateway_wildcard_hostname,
    request_inference,
    request_inference_with_jwt_warmup,
    wait_for_inferenceservice_ready,
)
from ingress import find_gateway_for_domain, gateway_service_name, get_service_lb_ip
from lightkube import Client, codecs
from utils import PROFILE_RESOURCE, assert_namespace_active, assert_resource_deleted

log = logging.getLogger(__name__)

# Assets directory is relative to the repository root.
ASSETS_DIR = Path(__file__).parent.parent.parent.parent / "assets"
PROFILE_TEMPLATE_FILE = ASSETS_DIR / "test-profile.yaml.j2"
INFERENCE_SERVICE_TEMPLATE_FILE = ASSETS_DIR / "kserve-inference-service.yaml.j2"

IAM_MODEL = "iam"
KUBEFLOW_MODEL = "kubeflow"
NAMESPACE = PROFILE_NAME = "test-m2m"
ISVC_NAME = "sklearn-v2-iris"
DOMAIN = "api.kubeflow.com"
WILDCARD_HOSTNAME = f"*.{DOMAIN}"

# The prediction request body sent to the sklearn v2 iris model.
PAYLOAD = '{"instances": [[6.8, 2.8, 4.8, 1.4], [6.0, 3.4, 4.5, 1.6]]}'

# Interval the kubeflow model's update-status hook is throttled to during the test run
# to stop github-profiles-automator from reconciling mid-test (see
# github-profiles-automator-bug.md).
UPDATE_STATUS_HOOK_INTERVAL = "2h"


@pytest.fixture(scope="module", autouse=True)
def slow_update_status_hook():
    """Raise the ``kubeflow`` model's update-status interval for the test run.

    ``github-profiles-automator`` runs a full reconcile on every ``update_status``
    hook and revokes access to Profiles absent from its PMR, which deletes the
    RoleBinding/AuthorizationPolicy these tests create and makes them flaky. Setting
    the interval to 2h stops the hook from firing mid-test; the original value is
    restored on teardown.
    """
    juju = jubilant.Juju(model=KUBEFLOW_MODEL)
    original = juju.model_config().get("update-status-hook-interval")
    log.info(
        f"Setting '{KUBEFLOW_MODEL}' update-status-hook-interval to "
        f"{UPDATE_STATUS_HOOK_INTERVAL} (was {original})"
    )
    juju.model_config({"update-status-hook-interval": UPDATE_STATUS_HOOK_INTERVAL})

    yield

    if original:
        log.info(f"Restoring '{KUBEFLOW_MODEL}' update-status-hook-interval to {original}")
        juju.model_config({"update-status-hook-interval": original})


@pytest.fixture(scope="module")
def m2m_gateway(lightkube_client: Client) -> str:
    """Name of the istio Gateway serving the KServe (M2M) domain.

    Discovered dynamically by matching the listener hostname, so the charm/app name
    does not need to be hardcoded.
    """
    return find_gateway_for_domain(lightkube_client, KUBEFLOW_MODEL, DOMAIN)


@pytest.fixture(scope="module")
def gateway_principals(m2m_gateway: str) -> list[str]:
    """Istio principal of the M2M ingress gateway serving KServe."""
    return [f"cluster.local/ns/{KUBEFLOW_MODEL}/sa/{gateway_service_name(m2m_gateway)}"]


@pytest.fixture(scope="module")
def gateway_ip(lightkube_client: Client, m2m_gateway) -> str:
    """LoadBalancer IP of the M2M ingress gateway."""
    return get_service_lb_ip(lightkube_client, KUBEFLOW_MODEL, gateway_service_name(m2m_gateway))


@pytest.fixture(scope="module")
def issuer_url() -> str:
    """JWT issuer URL trusted by the gateway's RequestAuthentication."""
    return get_jwt_issuer_url(KUBEFLOW_MODEL)


@pytest.fixture(scope="module")
def patch_gateway(lightkube_client: Client, m2m_gateway: str):
    """Patch the M2M Gateway listeners to a wildcard hostname.

    Workaround for https://github.com/canonical/service-mesh/issues/102 so KServe's
    per-service subdomain routes attach to the gateway. Remove this fixture once the
    issue is fixed and the charm supports wildcard listeners natively.
    """
    patch_gateway_wildcard_hostname(
        lightkube_client, KUBEFLOW_MODEL, m2m_gateway, WILDCARD_HOSTNAME
    )
    yield


@pytest.fixture()
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
def create_inference_service(
    lightkube_client: Client, create_profile, patch_gateway, keep_artifacts: bool
):
    """Create the KServe InferenceService and return its hostname."""
    profile_name, profile_uuid = create_profile
    isvc_name = f"{ISVC_NAME}-{profile_uuid}"
    log.info(f"Creating InferenceService {profile_name}/{isvc_name}...")
    resources = list(
        codecs.load_all_yaml(
            INFERENCE_SERVICE_TEMPLATE_FILE.read_text(),
            context={"name": isvc_name, "namespace": profile_name},
        )
    )
    assert len(resources) == 1, f"Expected 1 InferenceService, got {len(resources)}!"
    lightkube_client.create(resources[0])

    hostname = wait_for_inferenceservice_ready(lightkube_client, isvc_name, profile_name)

    yield hostname, isvc_name

    if keep_artifacts:
        log.info(f"Keeping InferenceService {profile_name}/{isvc_name} (--keep-artifacts set)")
        return

    assert_resource_deleted(lightkube_client, INFERENCE_SERVICE_RESOURCE, isvc_name, profile_name)


@pytest.fixture()
def authorized_client(
    lightkube_client: Client, create_profile, gateway_principals, keep_artifacts: bool
):
    """Create an OAuth client and authorize it as a contributor on the Profile."""
    profile_name, _ = create_profile
    client_id, client_secret = create_oauth_client(IAM_MODEL, "uat-m2m-authorized")
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
def unauthorized_client(keep_artifacts: bool):
    """Create an OAuth client that is NOT authorized on any Profile."""
    client_id, client_secret = create_oauth_client(IAM_MODEL, "uat-m2m-unauthorized")

    yield client_id, client_secret

    if keep_artifacts:
        return
    delete_oauth_client(IAM_MODEL, client_id)


@pytest.fixture()
def authorized_token(authorized_client, issuer_url) -> str:
    """A valid access token for the authorized OAuth client."""
    client_id, client_secret = authorized_client
    return get_token(client_id, client_secret, issuer_url)


@pytest.fixture(scope="module")
def unauthorized_token(unauthorized_client, issuer_url):
    """A valid access token for the unauthorized OAuth client."""
    client_id, client_secret = unauthorized_client
    return get_token(client_id, client_secret, issuer_url)


def test_authorized_token_reaches_inferenceservice(
    authorized_token: str, create_inference_service, gateway_ip: str
):
    """A valid token from an authorized client reaches the InferenceService.

    This confirms the full path: Hydra issues the token, the gateway's
    RequestAuthentication validates the issuer, the Profile's AuthorizationPolicy
    authorizes the client identity, and KServe serves the inference.
    """
    hostname, isvc_name = create_inference_service

    http_code, body = request_inference_with_jwt_warmup(
        hostname, gateway_ip, authorized_token, PAYLOAD, isvc_name
    )

    assert http_code == 200, f"Expected HTTP 200, got {http_code}. Body: {body}"
    assert "predictions" in body, f"Expected a prediction in the response, got: {body}"
    log.info("✓ Authorized token successfully reached the InferenceService.")


def test_missing_token_is_rejected(create_inference_service, gateway_ip: str):
    """A request without a token is denied by the AuthorizationPolicy (403).

    A token-less request carries no identity. RequestAuthentication does not reject
    it (it only 401s present-but-invalid/expired tokens), so it reaches the
    AuthorizationPolicy, where no rule matches and the request is denied with 403
    (RBAC: access denied).
    """
    hostname, isvc_name = create_inference_service

    http_code, body = request_inference(hostname, gateway_ip, None, PAYLOAD, isvc_name)

    assert (
        http_code == 403
    ), f"Expected HTTP 403 for a request without a token, got {http_code}. Body: {body}"
    log.info("✓ Request without a token was correctly denied.")


def test_invalid_token_is_rejected(create_inference_service, gateway_ip: str):
    """A request with an invalid token is rejected by RequestAuthentication."""
    hostname, isvc_name = create_inference_service

    http_code, body = request_inference(
        hostname, gateway_ip, "not-a-valid-jwt", PAYLOAD, isvc_name
    )

    assert (
        http_code == 401
    ), f"Expected HTTP 401 for an invalid token, got {http_code}. Body: {body}"
    log.info("✓ Request with an invalid token was correctly rejected.")


def test_unauthorized_token_is_forbidden(
    unauthorized_token: str, create_inference_service, gateway_ip: str
):
    """A valid token from an unauthorized client is forbidden by the AuthorizationPolicy.

    The token is authentic (issued by Hydra) but its client identity is not a
    contributor on the Profile, so the request is denied with RBAC access denied.
    """
    hostname, isvc_name = create_inference_service

    http_code, body = request_inference_with_jwt_warmup(
        hostname, gateway_ip, unauthorized_token, PAYLOAD, isvc_name
    )

    assert http_code == 403, (
        f"Expected HTTP 403 (RBAC: access denied) for an unauthorized client, "
        f"got {http_code}. Body: {body}"
    )
    log.info("✓ Valid token from an unauthorized client was correctly forbidden.")
