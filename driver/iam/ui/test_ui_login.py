# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""UI (Identity login) integration tests.

These tests log in to the Kubeflow dashboard through the Canonical Identity
Platform (IdP) using a headless Chromium browser, and assert the dashboard loads.

They assume a Kubeflow + Identity Platform deployment with an ambient service mesh
(e.g. the ``kubeflow-ambient-iam`` setup in charmed-kubeflow-solutions): Juju models
``iam``, ``iam-core`` and ``kubeflow``, a Kratos identity, the
``identity-platform-login-ui`` behind a ``traefik-lb`` Service, and an istio ingress
gateway serving ``ui.kubeflow.com``.
"""

import logging
import time
from pathlib import Path

import jubilant
import pytest
from iam.ui.helpers import (
    AUTH_DOMAIN,
    IAM_MODEL,
    KUBEFLOW_MODEL,
    UI_DOMAIN,
    build_host_resolver_rules,
    create_kratos_user,
    get_auth_lb_ip,
    get_ui_lb_ip,
    goto_login_form,
    is_auth_url,
    is_ui_url,
    login_and_reach_dashboard,
    remove_kratos_user,
)
from lightkube import Client, codecs
from playwright.sync_api import sync_playwright
from utils import PROFILE_RESOURCE, assert_namespace_active, assert_resource_deleted

log = logging.getLogger(__name__)

# Assets directory is relative to the repository root.
ASSETS_DIR = Path(__file__).parent.parent.parent.parent / "assets"
PROFILE_TEMPLATE_FILE = ASSETS_DIR / "test-profile.yaml.j2"

# Directory for failure artifacts (Playwright trace + screenshot). Uses a fixed path
# relative to the repo root so CI can reliably upload them.
ARTIFACTS_DIR = Path(__file__).parent.parent.parent.parent / "playwright-artifacts"

NAMESPACE = "test-ui-iam"

# Interval the kubeflow model's update-status hook is throttled to during the test run
# to stop github-profiles-automator from reconciling mid-test (see
# github-profiles-automator-bug.md).
UPDATE_STATUS_HOOK_INTERVAL = "2h"


@pytest.fixture(scope="module", autouse=True)
def slow_update_status_hook():
    """Raise the ``kubeflow`` model's update-status interval for the test run.

    ``github-profiles-automator`` runs a full reconcile on every ``update_status``
    hook and revokes access to Profiles absent from its PMR, which deletes the
    RoleBinding/AuthorizationPolicy backing the test user's Profile and makes login
    flaky. Setting the interval to 2h stops the hook from firing mid-test; the original
    value is restored on teardown.
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
def ui_ip(lightkube_client):
    """LoadBalancer IP of the istio Gateway serving the Kubeflow UI."""
    return get_ui_lb_ip(lightkube_client)


@pytest.fixture(scope="module")
def auth_ip(lightkube_client):
    """LoadBalancer IP of the IdP (auth) ingress."""
    return get_auth_lb_ip(lightkube_client)


@pytest.fixture(scope="module")
def playwright():
    """Start a Playwright instance for the module."""
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="module")
def browser(playwright, ui_ip, auth_ip):
    """Launch headless Chromium with host-resolver-rules pointing the in-cluster
    domains at their discovered LoadBalancer IPs (no ``/etc/hosts`` / root needed)."""
    args = [
        "--no-sandbox",
        "--host-resolver-rules=" + build_host_resolver_rules(ui_ip, auth_ip),
    ]
    browser = playwright.chromium.launch(headless=True, args=args)
    yield browser
    browser.close()


@pytest.fixture(scope="function")
def context(browser, request):
    """A fresh browser context per test so the negative test has no session.

    ``ignore_https_errors`` mirrors tenant-service's ``ignoreHTTPSErrors: true`` (the
    ingress serves self-signed certs). Browser console messages and page errors are
    forwarded to the pytest log so failures can be diagnosed from the output alone.
    On failure a Playwright trace (DOM snapshots, network log, sources) and a
    screenshot are also saved as artifacts under ``playwright-artifacts/``.
    """
    context = browser.new_context(ignore_https_errors=True)
    context.tracing.start(screenshots=True, snapshots=True, sources=True)

    page = context.new_page()
    page.on("console", lambda msg: log.info(f"[browser:{msg.type}] {msg.text}"))
    page.on("pageerror", lambda err: log.error(f"[browser:pageerror] {err}"))

    yield context

    failed = getattr(request.node, "rep_call", None) and request.node.rep_call.failed
    if failed:
        ARTIFACTS_DIR.mkdir(exist_ok=True)
        trace_path = ARTIFACTS_DIR / f"{request.node.name}.zip"
        context.tracing.stop(path=str(trace_path))
        log.info(f"Test failed; trace saved to {trace_path}")
        for i, p in enumerate(context.pages):
            screenshot_path = ARTIFACTS_DIR / f"{request.node.name}-page{i}.png"
            try:
                p.screenshot(path=str(screenshot_path))
                log.info(f"Screenshot saved to {screenshot_path}")
            except Exception as error:
                log.warning(f"Could not capture failure screenshot: {error}")
    else:
        context.tracing.stop()
    context.close()


@pytest.fixture(scope="module")
def iam_juju():
    """A Jubilant handle to the IAM model (where Kratos lives)."""
    return jubilant.Juju(model=IAM_MODEL)


@pytest.fixture(scope="module")
def kratos_user(iam_juju, keep_artifacts: bool):
    """Create a Kratos user + Juju secret; yield its credentials; clean up both.

    A unique username/email is generated per run.
    """
    stamp = str(int(time.time()))
    username = f"uat-ui-{stamp}"
    email = f"{username}@kubeflow-uats.local"
    password = "Uat-Ui-Pass-1234!"

    identity_id, secret_uri = create_kratos_user(iam_juju, username, email, password)

    yield username, email, password, identity_id, secret_uri

    if keep_artifacts:
        log.info(f"Keeping Kratos user {email} and secret {secret_uri} (--keep-artifacts set)")
        return

    remove_kratos_user(iam_juju, identity_id, secret_uri)


@pytest.fixture(scope="module")
def create_profile(lightkube_client: Client, kratos_user, keep_artifacts: bool):
    """Create a Profile owned by the Kratos user, then clean it up.

    The Profile owner must be the user's **email**, not their username: the UI
    gateway's RequestAuthentication forwards the JWT ``email`` claim to the
    ``kubeflow-userid`` header, so the dashboard queries namespaces by email.
    """
    email = kratos_user[1]
    log.info(f"Creating Profile {NAMESPACE} owned by {email}...")
    resources = list(
        codecs.load_all_yaml(
            PROFILE_TEMPLATE_FILE.read_text(),
            context={"namespace": NAMESPACE, "owner_name": email},
        )
    )
    assert len(resources) == 1, f"Expected 1 Profile, got {len(resources)}!"
    lightkube_client.create(resources[0])

    assert_namespace_active(lightkube_client, NAMESPACE)

    yield NAMESPACE

    if keep_artifacts:
        log.info(f"Keeping Profile {NAMESPACE} (--keep-artifacts set)")
        return

    # delete the Profile at the end of the module tests
    assert_resource_deleted(lightkube_client, PROFILE_RESOURCE, NAMESPACE)


def test_unauthenticated_request_is_redirected_to_login(context):
    """An unauthenticated request to the UI is redirected to the IdP login page."""
    page = context.pages[0]
    goto_login_form(page)
    page.get_by_role("heading", name="Sign in").wait_for(state="visible", timeout=60_000)

    assert is_auth_url(page.url), f"Expected to land on {AUTH_DOMAIN}, got {page.url}"
    log.info("✓ Unauthenticated request was redirected to the IdP login page.")


def test_login_reaches_dashboard(context, kratos_user, create_profile):
    """A valid IdP login reaches the Kubeflow central dashboard."""
    _, email, password, _, _ = kratos_user

    page = context.pages[0]
    login_and_reach_dashboard(page, email, password, profile_namespace=NAMESPACE)

    assert is_ui_url(page.url), f"Expected dashboard on host {UI_DOMAIN}, got {page.url}"
    log.info("✓ Login reached the Kubeflow dashboard.")
