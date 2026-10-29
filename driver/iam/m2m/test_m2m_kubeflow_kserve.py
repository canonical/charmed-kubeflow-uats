# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""M2M (machine-to-machine) identity integration tests for KServe with Kubeflow.

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

import pytest
from lightkube import Client, codecs

from helpers import (  # isort:skip
    ASSETS_DIR,
    INFERENCE_SERVICE_RESOURCE,
    request_inference,
    request_inference_with_jwt_warmup,
    wait_for_inferenceservice_ready,
)
from ingress import (  # isort:skip
    get_kserve_gateway,
    remove_gateway_listener_hostnames,
    restore_gateway_listener_hostnames,
)
from utils import assert_resource_deleted  # isort:skip

log = logging.getLogger(__name__)

INFERENCE_SERVICE_TEMPLATE_FILE = ASSETS_DIR / "kserve-inference-service.yaml.j2"

ISVC_NAME = "sklearn-v2-iris"

# The prediction request body sent to the sklearn v2 iris model.
PAYLOAD = '{"instances": [[6.8, 2.8, 4.8, 1.4], [6.0, 3.4, 4.5, 1.6]]}'


@pytest.fixture(scope="module")
def m2m_gateway(lightkube_client: Client, application_model: str) -> str:
    """Name of the istio Gateway KServe (M2M) attaches its InferenceService routes to.

    Overrides the conftest fixture to read it from KServe's ingress configuration rather
    than match it by listener hostname, so the Gateway is still found after
    ``patch_gateway`` removes its listener hostnames.
    """
    kserve_gateway = get_kserve_gateway(lightkube_client, application_model)
    assert kserve_gateway, "KServe is not configured with a Gateway API ingress gateway"
    gateway_namespace, gateway = kserve_gateway
    assert (
        gateway_namespace == application_model
    ), f"Expected the KServe Gateway in {application_model}, found {gateway_namespace}/{gateway}"
    log.info(f"Discovered KServe ingress Gateway '{gateway}'")
    return gateway


@pytest.fixture(scope="module")
def patch_gateway(lightkube_client: Client, m2m_gateway: str, application_model: str):
    """Remove the listener hostnames of the M2M Gateway for the whole module.

    Module-scoped override of the root ``patch_gateway`` fixture (see it for the
    workaround), as the InferenceService is shared by all the tests in the module.
    """
    removed = remove_gateway_listener_hostnames(lightkube_client, application_model, m2m_gateway)
    yield
    restore_gateway_listener_hostnames(lightkube_client, application_model, m2m_gateway, removed)


@pytest.fixture(scope="module")
def create_inference_service(
    lightkube_client: Client,
    create_profile,
    patch_gateway,
    keep_artifacts: bool,
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
