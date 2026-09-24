# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Helpers for the M2M (machine-to-machine) identity integration tests.

These helpers obtain a JWT from Hydra via the `client_credentials` grant and use it
to reach a KServe InferenceService through the istio ingress gateway from outside the
cluster.
"""

import json
import logging
import re
import socket
from contextlib import contextmanager

import jubilant
import requests
import tenacity
import urllib3
from ingress import GATEWAY_RESOURCE
from lightkube import Client
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.rbac_authorization_v1 import RoleBinding
from lightkube.types import PatchType
from oauthlib.oauth2 import BackendApplicationClient
from requests_oauthlib import OAuth2Session

log = logging.getLogger(__name__)

# Disable the noisy warnings emitted when talking to the self-signed endpoints.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Generic KServe InferenceService resource.
INFERENCE_SERVICE_RESOURCE = create_namespaced_resource(
    group="serving.kserve.io",
    version="v1beta1",
    kind="InferenceService",
    plural="inferenceservices",
)

# Generic Istio AuthorizationPolicy resource (mirrors github-profiles-automator).
AUTHORIZATION_POLICY_RESOURCE = create_namespaced_resource(
    group="security.istio.io",
    version="v1beta1",
    kind="AuthorizationPolicy",
    plural="authorizationpolicies",
)


def to_rfc1123_compliant(name: str) -> str:
    """Return an RFC1123-compliant version of the given name.

    Lower-cases the input, replaces any character that is not a lower-case
    alphanumeric or a hyphen with a hyphen, and strips leading/trailing hyphens.
    """
    sanitised = re.sub(r"[^a-z0-9-]", "-", name.lower())
    return sanitised.strip("-")


def create_oauth_client(iam_model: str, name: str) -> tuple[str, str]:
    """Create a Hydra OAuth client for the ``client_credentials`` grant.

    Args:
        iam_model: The Juju model where Hydra is deployed.
        name: A human-friendly name for the OAuth client.

    Returns:
        A ``(client_id, client_secret)`` tuple.
    """
    task = jubilant.Juju(model=iam_model).run(
        "hydra/0",
        "create-oauth-client",
        {
            "name": name,
            "grant-types": ["client_credentials"],
            "response-types": ["token"],
            "scope": ["openid"],
        },
    )
    client_id = task.results["client-id"]
    client_secret = task.results["client-secret"]
    log.info(f"Created OAuth client '{name}' with client-id {client_id}")
    return client_id, client_secret


def delete_oauth_client(iam_model: str, client_id: str) -> None:
    """Best-effort deletion of a Hydra OAuth client."""
    try:
        jubilant.Juju(model=iam_model).run(
            "hydra/0", "delete-oauth-client", {"client-id": client_id}
        )
        log.info(f"Deleted OAuth client {client_id}")
    except Exception as error:
        log.warning(f"Could not delete OAuth client {client_id}: {error}")


def get_jwt_issuer_url(kubeflow_model: str) -> str:
    """Return the JWT issuer URL trusted by the gateway's RequestAuthentication.

    The issuer is sourced from oauth2-proxy's ``get-extra-jwt-issuers`` action, whose
    result is a Python-style string encoding a list of dictionaries.
    """
    task = jubilant.Juju(model=kubeflow_model).run("oauth2-proxy/0", "get-extra-jwt-issuers")
    raw = task.results["extra-jwt-issuers"]
    issuers = json.loads(raw.replace("'", '"'))
    issuer_url = issuers[0]["oidc-issuer-url"]
    log.info(f"Discovered JWT issuer URL: {issuer_url}")
    return issuer_url


def get_token(client_id: str, client_secret: str, issuer_url: str) -> str:
    """Request a ``client_credentials`` access token from the issuer.

    Uses an OAuth2 client (``requests-oauthlib``) to run the ``client_credentials``
    grant against the token endpoint advertised by the issuer's OpenID configuration.

    Args:
        client_id: The OAuth client id.
        client_secret: The OAuth client secret.
        issuer_url: The OIDC issuer URL.

    Returns:
        The access token string.
    """
    discovery = requests.get(
        f"{issuer_url}/.well-known/openid-configuration", verify=False, timeout=30
    )
    discovery.raise_for_status()
    token_endpoint = discovery.json()["token_endpoint"]

    session = OAuth2Session(client=BackendApplicationClient(client_id=client_id))
    token = session.fetch_token(
        token_url=token_endpoint,
        client_id=client_id,
        client_secret=client_secret,
        scope=["openid"],
        verify=False,
    )
    log.info(f"Obtained access token for client {client_id}")
    return token["access_token"]


def patch_gateway_wildcard_hostname(
    client: Client, namespace: str, gateway: str, hostname: str
) -> None:
    """Patch every listener of a Gateway to use the given (wildcard) hostname.

    Workaround for https://github.com/canonical/service-mesh/issues/102: the
    istio-ingress-k8s charm pins each listener to the exact ``external_hostname``,
    which rejects KServe's per-service subdomain routes. Remove this once the issue
    is fixed and the charm supports wildcard listeners natively.
    """
    gw = client.get(GATEWAY_RESOURCE, name=gateway, namespace=namespace)
    listeners = (gw.spec or {}).get("listeners", [])
    patch = [
        {"op": "replace", "path": f"/spec/listeners/{index}/hostname", "value": hostname}
        for index in range(len(listeners))
    ]
    log.info(f"Patching Gateway {namespace}/{gateway} listeners to hostname {hostname}")
    client.patch(GATEWAY_RESOURCE, gateway, patch, namespace=namespace, patch_type=PatchType.JSON)


def _contributor_rolebinding(namespace: str, user: str, role: str) -> RoleBinding:
    """Build the RoleBinding a contributor would get from KFAM/github-profiles-automator."""
    name = to_rfc1123_compliant(f"{user}-{role}")
    return RoleBinding.from_dict(
        {
            "metadata": {
                "name": name,
                "namespace": namespace,
                "annotations": {"user": user, "role": role},
            },
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": f"kubeflow-{role}",
            },
            "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "User", "name": user}],
        }
    )


def _contributor_authorization_policy(namespace: str, user: str, role: str, principals: list[str]):
    """Build the ambient AuthorizationPolicy a contributor would get.

    Mirrors github-profiles-automator's
    ``generate_contributor_authorization_policy`` for the ambient case: the request
    must originate from one of the ingress gateway principals *and* carry the
    ``kubeflow-userid`` header matching the contributor (the OAuth client id).
    """
    name = to_rfc1123_compliant(f"{user}-{role}")
    return AUTHORIZATION_POLICY_RESOURCE.from_dict(
        {
            "metadata": {
                "name": name,
                "namespace": namespace,
                "annotations": {"user": user, "role": role},
            },
            "spec": {
                "rules": [
                    {
                        "from": [{"source": {"principals": principals}}],
                        "when": [
                            {
                                "key": "request.headers[kubeflow-userid]",
                                "values": [user],
                            }
                        ],
                    }
                ],
                "targetRefs": [
                    {
                        "group": "gateway.networking.k8s.io",
                        "kind": "Gateway",
                        "name": "waypoint",
                    }
                ],
            },
        }
    )


def authorize_contributor(
    client: Client, namespace: str, user: str, role: str, principals: list[str]
) -> None:
    """Grant a contributor access to a Profile namespace.

    Creates the RoleBinding and (ambient) AuthorizationPolicy that KFAM /
    github-profiles-automator would otherwise create from a PMR ``contributor`` entry.

    NOTE: We create the authorization directly instead of via github-profiles-automator
    (edit ``pmr.yaml`` -> automator reconciles), because this suite tests the
    enforcement path and the gating object is identical either way. Driving the PMR
    path is a deploy-time concern (the client_id can't be pinned, so it must be seeded
    into a test PMR repo by Terraform) and is best left to the automator's own tests.
    """
    log.info(f"Authorizing contributor '{user}' with role '{role}' on namespace {namespace}")
    client.apply(_contributor_rolebinding(namespace, user, role), field_manager="m2m-uats")
    client.apply(
        _contributor_authorization_policy(namespace, user, role, principals),
        field_manager="m2m-uats",
    )


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=2, min=5, max=60),
    stop=tenacity.stop_after_delay(60 * 10),
    reraise=True,
)
def wait_for_inferenceservice_ready(client: Client, name: str, namespace: str) -> str:
    """Wait for an InferenceService to become Ready and return its hostname.

    Returns:
        The InferenceService hostname (the URL with its scheme stripped).
    """
    isvc = client.get(INFERENCE_SERVICE_RESOURCE, name=name, namespace=namespace)
    status = isvc.status or {}
    conditions = status.get("conditions", [])
    ready_condition = next((c for c in conditions if c.get("type") == "Ready"), {})

    if ready_condition.get("status") != "True":
        reason = ready_condition.get("reason", "")
        message = ready_condition.get("message", "")
        log.info(
            f"Waiting for InferenceService {namespace}/{name} to become Ready "
            f"(reason={reason!r}, message={message!r})..."
        )
        raise AssertionError(
            f"InferenceService {namespace}/{name} is not Ready yet: {reason} {message}".strip()
        )

    url = status.get("url", "")
    hostname = url.split("://", 1)[-1].split("/", 1)[0]
    assert hostname, f"InferenceService {namespace}/{name} is Ready but has no URL"
    log.info(f"InferenceService {namespace}/{name} is Ready at {hostname}")
    return hostname


@contextmanager
def _pin_dns(hostname: str, ip: str):
    """Temporarily resolve ``hostname`` to ``ip`` for outgoing connections.

    This lets ``requests`` connect to the gateway LoadBalancer IP while still using
    ``hostname`` for the TLS SNI and Host header. There is no wildcard entry for the
    per-service subdomain in ``/etc/hosts``, so the name would not otherwise resolve.
    """
    original_getaddrinfo = socket.getaddrinfo

    def patched(host, *args, **kwargs):
        return original_getaddrinfo(ip if host == hostname else host, *args, **kwargs)

    socket.getaddrinfo = patched
    try:
        yield
    finally:
        socket.getaddrinfo = original_getaddrinfo


def request_inference(
    hostname: str, gateway_ip: str, token: str | None, payload: str, model_name: str
) -> tuple[int, str]:
    """Send an inference request to the InferenceService from outside the cluster.

    Connects to the gateway LoadBalancer IP (via a temporary DNS override) while
    presenting the correct TLS SNI and Host header for ``hostname``.

    Args:
        hostname: The InferenceService hostname.
        gateway_ip: The ingress gateway LoadBalancer IP to connect to.
        token: The bearer token to send, or ``None`` to omit the Authorization header.
        payload: The JSON request body.
        model_name: The served model name, used to build the predict URL.

    Returns:
        A ``(http_status_code, response_body)`` tuple.
    """
    url = f"https://{hostname}/v1/models/{model_name}:predict"
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"

    with _pin_dns(hostname, gateway_ip):
        response = requests.post(url, data=payload, headers=headers, verify=False, timeout=60)

    log.info(f"Inference request to {hostname} returned HTTP {response.status_code}")
    return response.status_code, response.text


# Warm-up bounds for gateway JWT verification on a cold-started deployment.
#
# Hydra creates the ``hydra.jwt.access-token`` signing key lazily, the first time a
# JWT access token is ever issued. If the M2M suite is that first consumer, the key is
# minted *after* the ingress gateway's RequestAuthentication was configured, so the
# gateway has no key matching the token's ``kid`` and rejects it with HTTP 401 ("Jwt
# verification fails"). The gateway recovers once its JWKS is refreshed to include the
# new key, so we retry the request until that convergence happens.
JWT_WARMUP_TIMEOUT = 360  # seconds to keep retrying while a valid token fails to verify
JWT_WARMUP_INTERVAL = 15  # seconds between attempts


def _jwt_not_yet_verified(result: tuple[int, str]) -> bool:
    """Retry predicate: a real token that the gateway has not (yet) verified.

    Only the tests that send a genuine token use this, so an HTTP 401 always means the
    gateway could not verify the signature -- expected transiently until its JWKS
    catches up with Hydra's access-token key. Any other status means verification
    completed (allowed, or authz-denied), so stop retrying.
    """
    code, _ = result
    return code == 401


def _log_jwt_warmup(retry_state: tenacity.RetryCallState) -> None:
    """Log each warm-up retry so a gateway whose JWKS never converges is visible."""
    code, body = retry_state.outcome.result()
    log.warning(
        f"Gateway returned HTTP {code} -- token not yet verified (gateway JWKS likely "
        f"still missing Hydra's access-token key). Retrying (attempt "
        f"{retry_state.attempt_number})... Body: {body!r}"
    )


def request_inference_with_jwt_warmup(
    hostname: str, gateway_ip: str, token: str | None, payload: str, model_name: str
) -> tuple[int, str]:
    """Send an inference request, tolerating the gateway's JWT verification warm-up.

    On a freshly deployed cluster, Hydra mints the ``hydra.jwt.access-token`` signing
    key only when the first JWT access token is issued -- which can happen after the
    gateway's RequestAuthentication was already configured. Until the gateway refreshes
    its JWKS, a valid token transiently fails signature verification (HTTP 401). Retry
    the request until the token verifies (any non-401 response) or the warm-up deadline
    elapses, then return the final ``(status_code, body)`` for the caller to assert on.
    """
    retryer = tenacity.Retrying(
        retry=tenacity.retry_if_result(_jwt_not_yet_verified),
        wait=tenacity.wait_fixed(JWT_WARMUP_INTERVAL),
        stop=tenacity.stop_after_delay(JWT_WARMUP_TIMEOUT),
        before_sleep=_log_jwt_warmup,
        retry_error_callback=lambda retry_state: retry_state.outcome.result(),
    )
    return retryer(request_inference, hostname, gateway_ip, token, payload, model_name)
