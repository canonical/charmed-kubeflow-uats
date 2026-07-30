# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Helpers for the UI (Identity login) integration tests.

These helpers log in to the Kubeflow dashboard through the Canonical Identity
Platform (IdP) using a headless Chromium browser, and assert the dashboard loads.

The browser-launch + login-flow conventions are borrowed from
``canonical/tenant-service`` ``tests/browser`` (the Identity team's own specs):
``ignoreHTTPSErrors`` and ``--host-resolver-rules`` at launch, identifier-first
login split into ``enter_email`` / ``enter_password``.

Ingress-gateway discovery (``find_gateway_for_domain``, ``get_service_lb_ip``,
``gateway_service_account``, ``GATEWAY_RESOURCE``) is shared with the
``driver/m2m`` suite via ``driver/ingress.py``.
"""

import logging
from urllib.parse import urlparse

import jubilant
from ingress import find_gateway_for_domain, gateway_service_account, get_service_lb_ip
from lightkube import Client
from lightkube.resources.core_v1 import Service

log = logging.getLogger(__name__)

# Model / service constants.
IAM_MODEL = "iam"
KUBEFLOW_MODEL = "kubeflow"
IAM_CORE_MODEL = "iam-core"
AUTH_SERVICE = "traefik-lb"

# DNS / URL constants.
UI_DOMAIN = "ui.kubeflow.com"
# The IdP (traefik ingress in iam-core) serves the auth + login UI on this hostname
# (set via the external_auth_hostname Terraform var in the solutions repo). Both the
# oauth2-proxy auth endpoint and the identity-platform-login-ui live under it (e.g.
# "auth.kubeflow.com/ui/login").
AUTH_DOMAIN = "auth.kubeflow.com"
UI_URL = f"https://{UI_DOMAIN}"


def create_kratos_user(
    iam_juju: jubilant.Juju, username: str, email: str, password: str
) -> tuple[str, str]:
    """Create a Kratos admin identity and return ``(identity_id, secret_uri)``.

    The password is passed via a Juju secret (``password-secret-id``) granted to the
    ``kratos`` charm, then the ``create-admin-account`` action is run.

    NOTE: verify the exact action name + param keys against the deployed kratos charm
    revision at implementation time. Research: action ``create-admin-account``, params
    ``username``/``email``/``password-secret-id``, returns ``identity-id``.
    """
    secret_uri = iam_juju.add_secret(
        f"uat-ui-{username}",
        {"password": password},
    )
    iam_juju.grant_secret(secret_uri, "kratos")

    task = iam_juju.run(
        "kratos/0",
        "create-admin-account",
        {
            "username": username,
            "email": email,
            "password-secret-id": str(secret_uri),
        },
    )
    identity_id = task.results["identity-id"]
    log.info(f"Created Kratos identity '{username}' ({identity_id})")
    return identity_id, secret_uri


def remove_kratos_secret(iam_juju: jubilant.Juju, secret_uri: str) -> None:
    """Best-effort removal of the Juju secret backing a Kratos test user.

    The Kratos identity itself is **not** deleted (the charm exposes no delete
    action); this is acceptable in an ephemeral CI environment. Only the secret is
    cleaned up so it does not accumulate.
    """
    try:
        iam_juju.remove_secret(secret_uri)
        log.info(f"Removed Juju secret {secret_uri}")
    except Exception as error:
        log.warning(f"Could not remove Juju secret {secret_uri}: {error}")


def get_ui_lb_ip(client: Client) -> str:
    """Return the LoadBalancer IP of the istio Gateway serving the Kubeflow UI."""
    gateway = find_gateway_for_domain(client, KUBEFLOW_MODEL, UI_DOMAIN)
    return get_service_lb_ip(client, KUBEFLOW_MODEL, gateway_service_account(gateway))


def get_auth_lb_ip(client: Client) -> str:
    """Return the LoadBalancer IP of the IdP (auth) ingress.

    Tries the expected ``traefik-lb`` Service in ``iam-core`` first, then falls back
    to listing Services in ``iam-core`` and picking the first whose name contains
    ``traefik`` and exposes a LoadBalancer IP.
    """
    try:
        return get_service_lb_ip(client, IAM_CORE_MODEL, AUTH_SERVICE)
    except Exception as error:
        log.info(
            f"Could not find {IAM_CORE_MODEL}/{AUTH_SERVICE} LoadBalancer IP ({error}); "
            "falling back to listing Services."
        )
    for svc in client.list(Service, namespace=IAM_CORE_MODEL):
        if "traefik" not in (svc.metadata.name or ""):
            continue
        ingress = (svc.status.loadBalancer.ingress or []) if svc.status else []
        if ingress and ingress[0].ip:
            ip = ingress[0].ip
            log.info(f"Found auth LoadBalancer IP {ip} on Service {svc.metadata.name}")
            return ip
    raise AssertionError(
        f"No Service with a LoadBalancer IP containing 'traefik' found in {IAM_CORE_MODEL}"
    )


def build_host_resolver_rules(ui_ip: str, auth_ip: str) -> str:
    """Build Chromium ``--host-resolver-rules`` mapping the UI and auth domains to IPs.

    Mirrors tenant-service's ``--host-resolver-rules=MAP dex 127.0.0.1``. This avoids
    needing ``/etc/hosts`` entries (and thus root) on the UAT host: Chromium resolves
    the in-cluster domains to the discovered LoadBalancer IPs directly.

    The solutions repo's ``test_deployment.py:configure_dns()`` already patches CoreDNS
    and ``/etc/hosts`` so these domains resolve on the runner, but ``--host-resolver-rules``
    makes the tests self-sufficient and not dependent on that step.
    """
    return f"MAP {UI_DOMAIN} {ui_ip}, MAP {AUTH_DOMAIN} {auth_ip}"


def login_with_password(page, email: str, password: str) -> None:
    """Fill credentials and submit the identity-platform-login-ui form.

    The deployed ``identity-platform-login-ui`` renders a single-page form with an
    ``Email`` field (input ``name=identifier``), a ``Password`` field, and a single
    ``Sign in`` button — **not** the two-step identifier-first flow that
    ``canonical/tenant-service`` assumes. Verified against the
    ``kubeflow-ambient-iam`` deployment.
    """
    page.get_by_label("Email").fill(email)
    # The password <label> text is "PasswordReset password" (it wraps a "Reset
    # password" link), so label-matching is brittle; select by input type instead.
    page.locator("input[type='password']").fill(password)
    page.get_by_role("button", name="Sign in", exact=True).click()


def is_ui_url(url: str) -> bool:
    """Return True iff ``url`` is actually served by the UI host.

    Matching on the parsed hostname (not a substring) is important: the oauth2-proxy
    auth page URL embeds the original UI target as a redirect parameter (e.g.
    ``kubeflow.com/oauth2/auth?...redirect_uri=https://ui.kubeflow.com/...``), so a
    naive ``"ui.kubeflow.com" in url`` check would be satisfied *while still on the
    auth page* and could let the test pass even when login never completed.
    """
    return urlparse(url).hostname == UI_DOMAIN


def is_auth_url(url: str) -> bool:
    """Return True iff ``url`` is served by the IdP (auth) host.

    Hostname equality (not a substring) is essential: ``AUTH_DOMAIN`` (``auth.kubeflow.com``)
    shares the parent domain ``kubeflow.com`` with ``UI_DOMAIN`` (``ui.kubeflow.com``), so a
    substring check would be satisfied while still on the UI page.
    """
    return urlparse(url).hostname == AUTH_DOMAIN


def reach_dashboard(page, profile_namespace: str | None = None) -> None:
    """Wait for the post-login redirect back to the UI and the dashboard to render.

    Asserts the URL is served by ``ui.kubeflow.com`` and a stable dashboard element is
    present.

    The ``profile_namespace`` assertion is **best-effort**: it tries to find the
    namespace in the dashboard's namespace selector, falling back to a content search.
    If either proves too brittle this assertion is intended to be dropped (per user
    instruction) — keep ``test_login_reaches_dashboard`` asserting only URL host +
    dashboard element in that case.
    """
    # Hostname match (not substring) so the auth page's `rd=...ui.kubeflow.com...`
    # redirect parameter cannot satisfy the wait before the dashboard is reached.
    page.wait_for_url(is_ui_url, timeout=120_000)

    # Stable dashboard element: the central dashboard renders a "Welcome" heading.
    # Verified against the kubeflow-ambient-iam deployment's central-dashboard. A
    # generic `heading.first` fallback is deliberately avoided: the IdP auth page also
    # has a "Sign in" heading, which would let a stuck auth page pass.
    page.get_by_role("heading", name="Welcome").wait_for(state="visible", timeout=60_000)

    log.info(f"Dashboard loaded at {page.url}")

    if profile_namespace:
        _assert_profile_visible(page, profile_namespace)


def _assert_profile_visible(page, profile_namespace: str) -> None:
    """Best-effort assertion that the user's profile namespace is selectable.

    Tries the namespace dropdown selector; on failure falls back to checking the
    namespace appears anywhere in the rendered page content. Any exception here is
    logged and swallowed (this assertion is droppable per user instruction).
    """
    try:
        page.get_by_role("button", name=profile_namespace).wait_for(
            state="visible", timeout=30_000
        )
        log.info(f"Profile namespace '{profile_namespace}' is selectable in the dashboard")
        return
    except Exception:
        log.info(
            f"Namespace selector for '{profile_namespace}' not found; trying content fallback"
        )

    try:
        content = page.content()
        if profile_namespace in content:
            log.info(f"Profile namespace '{profile_namespace}' found in page content")
            return
    except Exception:
        pass

    log.warning(
        f"Could not confirm profile namespace '{profile_namespace}' visibility; "
        "this assertion is best-effort and is being ignored."
    )
