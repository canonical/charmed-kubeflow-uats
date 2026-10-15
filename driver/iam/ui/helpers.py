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
``gateway_service_name``, ``GATEWAY_RESOURCE``) is shared with the
``driver/iam/m2m`` suite via ``driver/ingress.py``.
"""

import logging
import time
from functools import partial
from typing import Callable
from urllib.parse import urlparse

import jubilant
import tenacity
from ingress import find_gateway_for_domain, gateway_service_name, get_service_lb_ip
from lightkube import Client
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

log = logging.getLogger(__name__)

# Model / service constants.
IAM_MODEL = "iam"
IAM_CORE_MODEL = "iam-core"
AUTH_SERVICE = "traefik-lb"

# The mlflow-server charm serves the MLflow UI behind the "/mlflow/" ingress prefix (rewritten
# to "/"), so external navigation must target "<ui-host>/mlflow/" — the gateway has no route at
# the bare host and returns 404 there.
MLFLOW_UI_PATH = "/mlflow/"

# Chromium network-error codes that are transient and safe to retry. ERR_NETWORK_CHANGED
# is raised when Chromium's NetworkChangeNotifier detects the host's network config
# changed while a request was in flight (common in CI/containers where netlink events or
# /etc/hosts patching fire spurious change events); the rest are similar transient drops.
TRANSIENT_NET_ERRORS = (
    "ERR_NETWORK_CHANGED",
    "ERR_NETWORK_IO_SUSPENDED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_ABORTED",
)

# Per-attempt budget for the post-login redirect to reach the UI host. A normal redirect
# completes in seconds; a transient network change that aborts it leaves the page hung
# (Chromium does not auto-resubmit), so cap the wait well under reach_dashboard's own
# 120s budget to fail fast and re-drive the login rather than stall the whole test.
REDIRECT_TIMEOUT_MS = 60_000


def create_kratos_user(
    iam_juju: jubilant.Juju, username: str, email: str, password: str
) -> tuple[str, str]:
    """Create a Kratos admin identity and return ``(identity_id, secret_uri)``.

    The password is passed via a Juju secret (``password-secret-id``) granted to the
    ``kratos`` charm, then the ``create-admin-account`` action is run. Tested on
    kratos revision 565 (latest/stable): action ``create-admin-account``, params
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


def remove_kratos_user(iam_juju: jubilant.Juju, identity_id: str, secret_uri: str) -> None:
    """Delete a Kratos identity and its backing Juju secret.

    Uses the ``delete-identity`` action on the kratos charm to remove the identity,
    then removes the Juju secret. Both are best-effort (errors are logged, not raised).
    """
    try:
        iam_juju.run("kratos/0", "delete-identity", {"identity-id": identity_id})
        log.info(f"Deleted Kratos identity {identity_id}")
    except Exception as error:
        log.warning(f"Could not delete Kratos identity {identity_id}: {error}")

    try:
        iam_juju.remove_secret(secret_uri)
        log.info(f"Removed Juju secret {secret_uri}")
    except Exception as error:
        log.warning(f"Could not remove Juju secret {secret_uri}: {error}")


def get_ui_lb_ip(client: Client, application_model: str, ui_domain) -> str:
    """Return the LoadBalancer IP of the istio Gateway serving the UI."""
    gateway = find_gateway_for_domain(client, application_model, ui_domain)
    return get_service_lb_ip(client, application_model, gateway_service_name(gateway))


def get_auth_lb_ip(client: Client) -> str:
    """Return the LoadBalancer IP of the IdP (auth) traefik ingress."""
    return get_service_lb_ip(client, IAM_CORE_MODEL, AUTH_SERVICE)


def build_host_resolver_rules(ui_ip: str, auth_ip: str, ui_domain: str, auth_domain: str) -> str:
    """Build Chromium ``--host-resolver-rules`` mapping the UI and auth domains to IPs.

    Mirrors tenant-service's ``--host-resolver-rules=MAP dex 127.0.0.1``. This avoids
    needing ``/etc/hosts`` entries (and thus root) on the UAT host: Chromium resolves
    the in-cluster domains to the discovered LoadBalancer IPs directly.

    The solutions repo's ``test_deployment.py:configure_dns()`` already patches CoreDNS
    and ``/etc/hosts`` so these domains resolve on the runner, but ``--host-resolver-rules``
    makes the tests self-sufficient and not dependent on that step.
    """
    return f"MAP {ui_domain} {ui_ip}, MAP {auth_domain} {auth_ip}"


def login_with_password(page: Page, email: str, password: str) -> None:
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


def is_ui_url(url: str, ui_domain) -> bool:
    """Return True iff ``url`` is actually served by the UI host.

    Matching on the parsed hostname (not a substring) is important: the oauth2-proxy
    auth page URL embeds the original UI target as a redirect parameter (e.g.
    ``kubeflow.com/oauth2/auth?...redirect_uri=https://ui.kubeflow.com/...``), so a
    naive ``"ui.kubeflow.com" in url`` check would be satisfied *while still on the
    auth page* and could let the test pass even when login never completed.
    """
    return urlparse(url).hostname == ui_domain


def is_auth_url(url: str, auth_domain: str) -> bool:
    """Return True iff ``url`` is served by the IdP (auth) host.

    Hostname equality (not a substring) is essential: ``auth.kubeflow.com`` shares the parent
    domain ``kubeflow.com`` with ``ui.kubeflow.com``, so a substring check would be satisfied
    while still on the UI page.
    """
    return urlparse(url).hostname == auth_domain


def _is_transient_net_error(error: Exception) -> bool:
    """Return True if ``error`` is a transient Chromium network error worth retrying."""
    return any(code in str(error) for code in TRANSIENT_NET_ERRORS)


def _run_with_net_retry(
    action: Callable[[], object], description: str, max_attempts: int = 3
) -> None:
    """Run ``action`` (a navigation), retrying transient Chromium network errors.

    Chromium aborts an in-flight request with ERR_NETWORK_CHANGED when the host's
    network configuration changes underneath it — frequent in CI/containers where
    netlink events or /etc/hosts churn trip its NetworkChangeNotifier. Navigations are
    idempotent, so retry them a few times before giving up.
    """

    def _log_retry(retry_state: tenacity.RetryCallState) -> None:
        log.warning(
            "%s hit a transient network error (attempt %d/%d): %s — retrying",
            description,
            retry_state.attempt_number,
            max_attempts,
            retry_state.outcome.exception(),
        )

    retryer = tenacity.Retrying(
        # Only Chromium's transient network drops are retryable; any other Playwright
        # error (or a real timeout) is re-raised on the first occurrence.
        retry=(
            tenacity.retry_if_exception_type(PlaywrightError)
            & tenacity.retry_if_exception(_is_transient_net_error)
        ),
        wait=tenacity.wait_fixed(2),
        stop=tenacity.stop_after_attempt(max_attempts),
        before_sleep=_log_retry,
        reraise=True,
    )
    retryer(action)


def goto_login_form(page: Page, ui_url: str, auth_domain: str, max_attempts: int = 3) -> None:
    """Navigate to the UI, follow the redirect to the IdP login form, and wait for it.

    Retries when the login-ui lands on its /ui/error page (e.g. Kratos briefly
    500s /self-service/login/browser while mid-restart on a CA-cert rotation),
    which otherwise leaves the SPA stuck on an error screen with no Email field.

    Waits for the ``Email`` input directly rather than the "Sign in" heading: the
    heading is server-rendered in the initial HTML and is present even on the error
    page, so it is not a reliable readiness signal.

    NOTE: The IdP (traefik ingress in iam-core) serves the auth + login UI on the same ui_url
    hostname (set via the external_auth_hostname Terraform var in the solutions repo). Both the
    oauth2-proxy auth endpoint and the identity-platform-login-ui live under it (e.g.,
    "auth.kubeflow.com/ui/login").
    """
    for attempt in range(1, max_attempts + 1):
        # goto follows the full redirect chain (ui.<domain> → oauth2-proxy →
        # auth.<domain>/ui/login) and returns only once the final page's load
        # event fires, so the Email label search runs on the login page, not the
        # UI page — no risk of matching a label before the redirect completes.
        _run_with_net_retry(partial(page.goto, ui_url), "Navigation to the UI")
        try:
            page.get_by_label("Email").wait_for(state="visible", timeout=30_000)
            return
        except PlaywrightTimeoutError:
            on_error_page = is_auth_url(page.url, auth_domain) and "/ui/error" in page.url
            if on_error_page and attempt < max_attempts:
                log.warning(
                    "Login-ui landed on /ui/error (attempt %d/%d); "
                    "likely a transient Kratos restart — retrying",
                    attempt,
                    max_attempts,
                )
                page.context.clear_cookies()
                time.sleep(10)
                continue
            raise


def reach_dashboard(page: Page, profile_namespace: str, max_attempts: int = 2) -> None:
    """Wait for the post-login redirect back to the UI and the dashboard to render.

    Asserts the URL is served by ``ui.kubeflow.com``, the dashboard page has loaded
    (title check), and the user's profile namespace is the active namespace in the URL.

    The central dashboard is a Polymer web-components app that uses Shadow DOM, so
    ``inner_text`` and ``get_by_role`` cannot pierce its shadow roots. The page title
    ("Kubeflow Central Dashboard") and the ``?ns=`` query parameter are used instead.

    A Profile must exist for the user before calling this — without one, the dashboard
    shows an intermediate "Welcome" page instead of the main view.
    """
    # Hostname match (not substring) so the auth page's `rd=...ui.kubeflow.com...`
    # redirect parameter cannot satisfy the wait before the dashboard is reached.
    page.wait_for_url(is_ui_url, timeout=120_000)

    def _render_dashboard() -> None:
        # Wait for the dashboard SPA to settle (API calls complete, namespace set).
        page.wait_for_load_state("networkidle", timeout=60_000)

        # Confirm the dashboard page loaded. The title is set in the static HTML so
        # it's available before the SPA fully hydrates, but combined with the
        # networkidle wait above it confirms the page actually rendered.
        assert (
            page.title() == "Kubeflow Central Dashboard"
        ), f"Expected dashboard title, got {page.title()!r}"

        log.info(f"Dashboard loaded at {page.url}")

        _assert_profile_visible(page, profile_namespace)

    def _reload_before_retry(retry_state: tenacity.RetryCallState) -> None:
        # A transient ERR_NETWORK_CHANGED can abort one of the SPA's resource/API
        # requests mid-hydration, leaving the dashboard half-rendered. Reloading
        # (the session cookie is already set, so no re-login) usually recovers.
        log.warning(
            "Dashboard did not settle (attempt %d/%d): %s — reloading",
            retry_state.attempt_number,
            max_attempts,
            retry_state.outcome.exception(),
        )
        _run_with_net_retry(page.reload, "Dashboard reload")

    retryer = tenacity.Retrying(
        # Only retry while still on the UI host: a URL that fell back to the auth host
        # is an aborted redirect, not a render glitch, and reloading will not fix it.
        retry=(
            tenacity.retry_if_exception_type((PlaywrightTimeoutError, AssertionError))
            & tenacity.retry_if_exception(lambda _error: is_ui_url(page.url))
        ),
        stop=tenacity.stop_after_attempt(max_attempts),
        before_sleep=_reload_before_retry,
        reraise=True,
    )
    retryer(_render_dashboard)


def _assert_profile_visible(page: Page, profile_namespace: str) -> None:
    """Assert that the user's profile namespace is the active namespace in the dashboard.

    Two signals are checked:

    1. The ``?ns=<namespace>`` query parameter is set in the URL (the SPA sets this
       client-side after hydration).
    2. The namespace appears in the dashboard UI (the namespace dropdown in the
       top-left corner). The dashboard is a Polymer app using Shadow DOM, but
       Playwright's ``get_by_text`` pierces open shadow roots, so this is a true UI
       assertion — not just an API or URL check.
    """
    assert (
        profile_namespace in page.url
    ), f"Expected ?ns={profile_namespace} in URL, got {page.url}"

    page.get_by_text(profile_namespace, exact=False).first.wait_for(
        state="visible", timeout=30_000
    )
    log.info(f"Profile namespace '{profile_namespace}' is visible in the dashboard")


def login_and_reach_kubeflow_dashboard(
    page: Page,
    email: str,
    password: str,
    profile_namespace: str,
    max_attempts: int = 3,
) -> None:
    """Drive the full IdP login flow to the dashboard, retrying transient network aborts.

    A transient ERR_NETWORK_CHANGED can abort the redirect chain kicked off by the login
    submit, stranding the browser on the auth host so it never reaches ``ui.kubeflow.com``.
    The abort surfaces either as a directly-raised ERR_NETWORK_CHANGED or as a timeout (the
    URL never changes); the flow is idempotent (the Kratos user and Profile already exist),
    so on either we clear cookies and drive it again from the login form. A failure once
    already on the UI host is a dashboard-render problem, not an aborted redirect, so it is
    re-raised rather than retried.
    """

    def _login_and_reach() -> None:
        login_with_password(page, email, password)
        # Wait for the post-login redirect to land on the UI host. A network change can
        # abort it two ways: wait_for_url times out (the URL never changes) or Playwright
        # surfaces the aborted navigation's ERR_NETWORK_CHANGED directly. Both are retried.
        page.wait_for_url(is_ui_url, timeout=REDIRECT_TIMEOUT_MS)
        reach_dashboard(page, profile_namespace)

    def _redirect_failed_off_ui_host(error: Exception) -> bool:
        """Return True if the redirect failed recoverably while still off the UI host.

        A transient network change breaks the post-login redirect either as a wait_for_url
        timeout (the URL never changed) or as a directly-raised ERR_NETWORK_CHANGED; both
        are recoverable by re-driving the idempotent login. Once on the UI host, a failure
        is a dashboard-render problem — don't retry.
        """
        if is_ui_url(page.url):
            return False
        return isinstance(error, PlaywrightTimeoutError) or _is_transient_net_error(error)

    def _redrive_login(retry_state: tenacity.RetryCallState) -> None:
        log.warning(
            "Login did not reach the dashboard (attempt %d/%d): %s — likely a "
            "transient network change aborted the redirect; "
            "clearing cookies and retrying",
            retry_state.attempt_number,
            max_attempts,
            retry_state.outcome.exception(),
        )
        page.context.clear_cookies()
        goto_login_form(page)

    retryer = tenacity.Retrying(
        # A timeout or transient network error while still on the auth host is an aborted
        # redirect, so re-drive the (idempotent) login; a failure once on the UI host is a
        # render problem — stop retrying and let reach_dashboard's failure propagate.
        retry=tenacity.retry_if_exception(_redirect_failed_off_ui_host),
        stop=tenacity.stop_after_attempt(max_attempts),
        before_sleep=_redrive_login,
        reraise=True,
    )
    goto_login_form(page)
    retryer(_login_and_reach)


def reach_mlflow_ui(page: Page, ui_domain: str) -> None:
    """Wait for the post-login redirect back to the UI host and the MLflow app to load.

    Unlike the Kubeflow dashboard, the MLflow UI needs no Profile/namespace: reaching the
    UI host proves the oauth2-proxy session was established (authentication succeeded), and
    the tracking server serves the MLflow SPA to any authenticated user — a workspace grant
    only gates the API calls the SPA makes for content, which this login-flow check does
    not exercise.
    """
    # Hostname match (not substring) so the auth page's redirect parameter cannot satisfy
    # the wait before the MLflow UI is actually reached.
    page.wait_for_url(lambda url: is_ui_url(url, ui_domain), timeout=120_000)
    page.wait_for_load_state("networkidle", timeout=60_000)

    # The MLflow SPA sets "<title>MLflow</title>" in its static HTML; combined with the
    # networkidle wait this confirms the MLflow app rendered rather than the auth page.
    title = page.title()
    assert "mlflow" in title.lower(), f"Expected the MLflow UI, got page title {title!r}"
    log.info(f"MLflow UI loaded at {page.url}")


def login_and_reach_mlflow_ui(
    page: Page,
    email: str,
    password: str,
    ui_domain: str,
    auth_domain: str,
    max_attempts: int = 3,
) -> None:
    """Drive the IdP login flow until the MLflow UI loads, retrying transient network aborts.

    Mirrors ``login_and_reach_kubeflow_dashboard`` but lands on the MLflow UI instead:
    a transient ERR_NETWORK_CHANGED can abort the post-login redirect and strand the browser
    on the auth host, so on a timeout or transient network error while still off the UI host
    the (idempotent) login is re-driven. A failure once already on the UI host is a render
    problem, not an aborted redirect, so it is re-raised.
    """
    ui_url = f"https://{ui_domain}{MLFLOW_UI_PATH}"

    def _login_and_reach() -> None:
        login_with_password(page, email, password)
        page.wait_for_url(lambda url: is_ui_url(url, ui_domain), timeout=REDIRECT_TIMEOUT_MS)
        reach_mlflow_ui(page, ui_domain)

    def _redirect_failed_off_ui_host(error: Exception) -> bool:
        if is_ui_url(page.url, ui_domain):
            return False
        return isinstance(error, PlaywrightTimeoutError) or _is_transient_net_error(error)

    def _redrive_login(retry_state: tenacity.RetryCallState) -> None:
        log.warning(
            "Login did not reach the MLflow UI (attempt %d/%d): %s — likely a transient "
            "network change aborted the redirect; clearing cookies and retrying",
            retry_state.attempt_number,
            max_attempts,
            retry_state.outcome.exception(),
        )
        page.context.clear_cookies()
        goto_login_form(page, ui_url=ui_url, auth_domain=auth_domain)

    retryer = tenacity.Retrying(
        retry=tenacity.retry_if_exception(_redirect_failed_off_ui_host),
        stop=tenacity.stop_after_attempt(max_attempts),
        before_sleep=_redrive_login,
        reraise=True,
    )
    goto_login_form(page, ui_url=ui_url, auth_domain=auth_domain)
    retryer(_login_and_reach)


def assert_unauthenticated_request_is_redirected_to_login(
    context, ui_domain, auth_domain, ui_path: str = "/"
):
    """Assert an unauthenticated request to the application UI is redirected to the IdP login."""
    page = context.pages[0]
    goto_login_form(page, ui_url=f"https://{ui_domain}{ui_path}", auth_domain=auth_domain)
    page.get_by_role("heading", name="Sign in").wait_for(state="visible", timeout=60_000)

    assert is_auth_url(page.url, auth_domain), f"Expected to land on {auth_domain}, got {page.url}"
    log.info("✓ Unauthenticated request was redirected to the IdP login page.")


def assert_login_reaches_application_ui(
    context,
    kratos_user,
    ui_domain: str,
    auth_domain: str,
    is_mlflow_standalone: bool,
    kubeflow_profile_name: str | None,
) -> None:
    """Assert that a valid IdP login reaches the application UI.

    For Kubeflow, a Profile owned by the testing Kratos user is precreated.

    For MLflow standalone, no MLflow workspace grant is needed: landing back on the UI host proves
    authentication succeeded, and the tracking server serves the MLflow app to any authenticated
    user.
    """
    _, email, password, _, _ = kratos_user

    page = context.pages[0]

    if is_mlflow_standalone:
        login_and_reach_mlflow_ui(page, email, password, ui_domain, auth_domain)
    else:  # i.e, for Kubeflow:
        login_and_reach_kubeflow_dashboard(
            page, email, password, profile_namespace=kubeflow_profile_name
        )

    assert is_ui_url(
        page.url, ui_domain
    ), f"Expected application UI on host {ui_domain}, got {page.url}"
    log.info("✓ Login reached the application UI.")
