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

import pytest
from helpers import (
    INFERENCE_SERVICE_RESOURCE,
    authorize_contributor,
    create_oauth_client,
    delete_oauth_client,
    find_gateway_for_domain,
    gateway_service_account,
    get_jwt_issuer_url,
    get_service_lb_ip,
    get_token,
    patch_gateway_wildcard_hostname,
    request_inference,
    wait_for_inferenceservice_ready,
)
from lightkube import ApiError, Client, codecs
from lightkube.generic_resource import load_in_cluster_generic_resources
from lightkube.types import CascadeType
from utils import PROFILE_RESOURCE, assert_namespace_active, assert_profile_deleted

log = logging.getLogger(__name__)

# Assets directory is relative to the repository root.
ASSETS_DIR = Path(__file__).parent.parent.parent / "assets"
PROFILE_TEMPLATE_FILE = ASSETS_DIR / "test-profile.yaml.j2"
INFERENCE_SERVICE_TEMPLATE_FILE = ASSETS_DIR / "kserve-inference-service.yaml.j2"

IAM_MODEL = "iam"
KUBEFLOW_MODEL = "kubeflow"
NAMESPACE = "test-m2m"
ISVC_NAME = "sklearn-v2-iris"
DOMAIN = "api.kubeflow.com"
WILDCARD_HOSTNAME = f"*.{DOMAIN}"

# The prediction request body sent to the sklearn v2 iris model.
PAYLOAD = '{"instances": [[6.8, 2.8, 4.8, 1.4], [6.0, 3.4, 4.5, 1.6]]}'


@pytest.fixture(scope="module")
def lightkube_client():
    """Initialise a Lightkube Client."""
    client = Client(trust_env=False)
    load_in_cluster_generic_resources(client)
    return client


@pytest.fixture(scope="module")
def m2m_gateway(lightkube_client):
    """Name of the istio Gateway serving the KServe (M2M) domain.

    Discovered dynamically by matching the listener hostname, so the charm/app name
    does not need to be hardcoded.
    """
    return find_gateway_for_domain(lightkube_client, KUBEFLOW_MODEL, DOMAIN)


@pytest.fixture(scope="module")
def gateway_principals(m2m_gateway):
    """Istio principal of the M2M ingress gateway serving KServe."""
    return [f"cluster.local/ns/{KUBEFLOW_MODEL}/sa/{gateway_service_account(m2m_gateway)}"]


@pytest.fixture(scope="module")
def gateway_ip(lightkube_client, m2m_gateway):
    """LoadBalancer IP of the M2M ingress gateway."""
    return get_service_lb_ip(
        lightkube_client, KUBEFLOW_MODEL, gateway_service_account(m2m_gateway)
    )


@pytest.fixture(scope="module")
def issuer_url():
    """JWT issuer URL trusted by the gateway's RequestAuthentication."""
    return get_jwt_issuer_url(KUBEFLOW_MODEL)


@pytest.fixture(scope="module")
def patch_gateway(lightkube_client, m2m_gateway):
    """Patch the M2M Gateway listeners to a wildcard hostname.

    Workaround for https://github.com/canonical/service-mesh/issues/102 so KServe's
    per-service subdomain routes attach to the gateway. Remove this fixture once the
    issue is fixed and the charm supports wildcard listeners natively.
    """
    patch_gateway_wildcard_hostname(
        lightkube_client, KUBEFLOW_MODEL, m2m_gateway, WILDCARD_HOSTNAME
    )
    yield


@pytest.fixture(scope="module")
def create_profile(lightkube_client):
    """Create the test Profile and clean it up at the end of the module."""
    log.info(f"Creating Profile {NAMESPACE}...")
    resources = list(
        codecs.load_all_yaml(
            PROFILE_TEMPLATE_FILE.read_text(),
            context={"namespace": NAMESPACE},
        )
    )
    assert len(resources) == 1, f"Expected 1 Profile, got {len(resources)}!"
    lightkube_client.create(resources[0])

    assert_namespace_active(lightkube_client, NAMESPACE)

    yield NAMESPACE

    log.info(f"Deleting Profile {NAMESPACE}...")
    try:
        lightkube_client.delete(PROFILE_RESOURCE, name=NAMESPACE, cascade=CascadeType.FOREGROUND)
        assert_profile_deleted(lightkube_client, NAMESPACE, log)
    except ApiError as error:
        if error.status.code != 404:
            raise
        log.info(f"Profile {NAMESPACE} already deleted")


@pytest.fixture(scope="module")
def create_inference_service(lightkube_client, create_profile, patch_gateway):
    """Create the KServe InferenceService and return its hostname."""
    log.info(f"Creating InferenceService {NAMESPACE}/{ISVC_NAME}...")
    resources = list(
        codecs.load_all_yaml(
            INFERENCE_SERVICE_TEMPLATE_FILE.read_text(),
            context={"name": ISVC_NAME, "namespace": NAMESPACE},
        )
    )
    assert len(resources) == 1, f"Expected 1 InferenceService, got {len(resources)}!"
    lightkube_client.create(resources[0])

    hostname = wait_for_inferenceservice_ready(lightkube_client, ISVC_NAME, NAMESPACE)

    yield hostname

    log.info(f"Deleting InferenceService {NAMESPACE}/{ISVC_NAME}...")
    try:
        lightkube_client.delete(INFERENCE_SERVICE_RESOURCE, name=ISVC_NAME, namespace=NAMESPACE)
    except ApiError as error:
        if error.status.code != 404:
            raise
        log.info(f"InferenceService {NAMESPACE}/{ISVC_NAME} already deleted")


@pytest.fixture(scope="module")
def authorized_client(lightkube_client, create_profile, gateway_principals):
    """Create an OAuth client and authorize it as a contributor on the Profile."""
    client_id, client_secret = create_oauth_client(IAM_MODEL, "uat-m2m-authorized")
    authorize_contributor(
        lightkube_client,
        namespace=NAMESPACE,
        user=client_id,
        role="edit",
        principals=gateway_principals,
    )

    yield client_id, client_secret

    delete_oauth_client(IAM_MODEL, client_id)


@pytest.fixture(scope="module")
def unauthorized_client():
    """Create an OAuth client that is NOT authorized on any Profile."""
    client_id, client_secret = create_oauth_client(IAM_MODEL, "uat-m2m-unauthorized")

    yield client_id, client_secret

    delete_oauth_client(IAM_MODEL, client_id)


@pytest.fixture(scope="module")
def authorized_token(authorized_client, issuer_url):
    """A valid access token for the authorized OAuth client."""
    client_id, client_secret = authorized_client
    return get_token(client_id, client_secret, issuer_url)


@pytest.fixture(scope="module")
def unauthorized_token(unauthorized_client, issuer_url):
    """A valid access token for the unauthorized OAuth client."""
    client_id, client_secret = unauthorized_client
    return get_token(client_id, client_secret, issuer_url)


def test_authorized_token_reaches_inferenceservice(
    create_inference_service, authorized_token, gateway_ip
):
    """A valid token from an authorized client reaches the InferenceService.

    This confirms the full path: Hydra issues the token, the gateway's
    RequestAuthentication validates the issuer, the Profile's AuthorizationPolicy
    authorizes the client identity, and KServe serves the inference.
    """
    hostname = create_inference_service

    http_code, body = request_inference(hostname, gateway_ip, authorized_token, PAYLOAD, ISVC_NAME)

    assert http_code == 200, f"Expected HTTP 200, got {http_code}. Body: {body}"
    assert "predictions" in body, f"Expected a prediction in the response, got: {body}"
    log.info("✓ Authorized token successfully reached the InferenceService.")


def test_missing_token_is_rejected(create_inference_service, gateway_ip):
    """A request without a token is denied by the AuthorizationPolicy (403).

    A token-less request carries no identity. RequestAuthentication does not reject
    it (it only 401s present-but-invalid/expired tokens), so it reaches the
    AuthorizationPolicy, where no rule matches and the request is denied with 403
    (RBAC: access denied).
    """
    hostname = create_inference_service

    http_code, body = request_inference(hostname, gateway_ip, None, PAYLOAD, ISVC_NAME)

    assert (
        http_code == 403
    ), f"Expected HTTP 403 for a request without a token, got {http_code}. Body: {body}"
    log.info("✓ Request without a token was correctly denied.")


def test_invalid_token_is_rejected(create_inference_service, gateway_ip):
    """A request with an invalid token is rejected by RequestAuthentication."""
    hostname = create_inference_service

    http_code, body = request_inference(
        hostname, gateway_ip, "not-a-valid-jwt", PAYLOAD, ISVC_NAME
    )

    assert (
        http_code == 401
    ), f"Expected HTTP 401 for an invalid token, got {http_code}. Body: {body}"
    log.info("✓ Request with an invalid token was correctly rejected.")


def test_unauthorized_token_is_forbidden(create_inference_service, unauthorized_token, gateway_ip):
    """A valid token from an unauthorized client is forbidden by the AuthorizationPolicy.

    The token is authentic (issued by Hydra) but its client identity is not a
    contributor on the Profile, so the request is denied with RBAC access denied.
    """
    hostname = create_inference_service

    http_code, body = request_inference(
        hostname, gateway_ip, unauthorized_token, PAYLOAD, ISVC_NAME
    )

    assert http_code == 403, (
        f"Expected HTTP 403 (RBAC: access denied) for an unauthorized client, "
        f"got {http_code}. Body: {body}"
    )
    log.info("✓ Valid token from an unauthorized client was correctly forbidden.")
