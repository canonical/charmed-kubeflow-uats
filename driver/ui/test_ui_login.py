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
from helpers import (
    AUTH_DOMAIN,
    IAM_MODEL,
    UI_DOMAIN,
    UI_URL,
    build_host_resolver_rules,
    create_kratos_user,
    get_auth_lb_ip,
    get_ui_lb_ip,
    is_auth_url,
    is_ui_url,
    login_with_password,
    reach_dashboard,
    remove_kratos_secret,
)
from lightkube import ApiError, Client, codecs
from lightkube.generic_resource import load_in_cluster_generic_resources
from lightkube.types import CascadeType
from playwright.sync_api import sync_playwright
from utils import PROFILE_RESOURCE, assert_namespace_active, assert_profile_deleted

log = logging.getLogger(__name__)

# Assets directory is relative to the repository root.
ASSETS_DIR = Path(__file__).parent.parent.parent / "assets"
PROFILE_TEMPLATE_FILE = ASSETS_DIR / "ui-profile.yaml.j2"

NAMESPACE = "test-ui-iam"


@pytest.fixture(scope="module")
def lightkube_client():
    """Initialise a Lightkube Client."""
    client = Client(trust_env=False)
    load_in_cluster_generic_resources(client)
    return client


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
def context(browser, tmp_path, request):
    """A fresh browser context per test so the negative test has no session.

    ``ignore_https_errors`` mirrors tenant-service's ``ignoreHTTPSErrors: true`` (the
    ingress serves self-signed certs). Browser console messages and page errors are
    forwarded to the pytest log so failures can be diagnosed from the output alone.
    On failure a Playwright trace (DOM snapshots, network log, sources) and a
    screenshot are also saved as artifacts; video recording is skipped as overkill.
    """
    context = browser.new_context(ignore_https_errors=True)
    context.tracing.start(screenshots=True, snapshots=True, sources=True)

    page = context.new_page()
    page.on("console", lambda msg: log.info(f"[browser:{msg.type}] {msg.text}"))
    page.on("pageerror", lambda err: log.error(f"[browser:pageerror] {err}"))

    yield context

    failed = getattr(request.node, "rep_call", None) and request.node.rep_call.failed
    if failed:
        trace_path = tmp_path / f"{request.node.name}.zip"
        context.tracing.stop(path=str(trace_path))
        log.info(f"Test failed; trace saved to {trace_path}")
        for p in context.pages:
            screenshot_path = tmp_path / f"{request.node.name}.png"
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
def kratos_user(iam_juju):
    """Create a Kratos user + Juju secret; yield its credentials; clean up the secret.

    The Kratos identity itself is not deleted (no delete action exists; acceptable in
    ephemeral CI). A unique username/email is generated per run.
    """
    stamp = str(int(time.time()))
    username = f"uat-ui-{stamp}"
    email = f"{username}@kubeflow-uats.local"
    password = "Uat-Ui-Pass-1234!"

    identity_id, secret_uri = create_kratos_user(iam_juju, username, email, password)

    yield username, email, password, identity_id, secret_uri

    remove_kratos_secret(iam_juju, secret_uri)


@pytest.fixture(scope="module")
def create_profile(lightkube_client, kratos_user):
    """Create a Profile owned by the Kratos user, then clean it up.

    BEST-EFFORT: if profile-namespace verification is dropped from
    ``reach_dashboard``, this fixture can be removed from the login test's
    dependencies too. It is kept so the namespace exists for the best-effort
    visibility check.
    """
    username = kratos_user[0]
    log.info(f"Creating Profile {NAMESPACE} owned by {username}...")
    resources = list(
        codecs.load_all_yaml(
            PROFILE_TEMPLATE_FILE.read_text(),
            context={"namespace": NAMESPACE, "owner": username},
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


def test_unauthenticated_request_is_redirected_to_login(context):
    """An unauthenticated request to the UI is redirected to the IdP login page."""
    page = context.pages[0]
    page.goto(UI_URL)

    # oauth2-proxy forward-auth redirects to the IdP login UI on kubeflow.com.
    page.wait_for_url(is_auth_url, timeout=120_000)
    page.get_by_role("heading", name="Sign in").wait_for(state="visible", timeout=60_000)

    assert is_auth_url(page.url), f"Expected to land on {AUTH_DOMAIN}, got {page.url}"
    log.info("✓ Unauthenticated request was redirected to the IdP login page.")


def test_login_reaches_dashboard(context, kratos_user, create_profile):
    """A valid IdP login reaches the Kubeflow central dashboard."""
    _, email, password, _, _ = kratos_user

    page = context.pages[0]
    page.goto(UI_URL)

    # Wait for the IdP login page to render before entering credentials.
    page.wait_for_url(is_auth_url, timeout=120_000)
    page.get_by_role("heading", name="Sign in").wait_for(state="visible", timeout=60_000)

    login_with_password(page, email, password)
    reach_dashboard(page, profile_namespace=NAMESPACE)

    assert is_ui_url(page.url), f"Expected dashboard on host {UI_DOMAIN}, got {page.url}"
    log.info("✓ Login reached the Kubeflow dashboard.")
