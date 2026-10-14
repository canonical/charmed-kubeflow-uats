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
from pathlib import Path

import pytest
from iam.ui.helpers import (
    build_host_resolver_rules,
    get_ui_lb_ip,
    goto_login_form,
    is_auth_url,
    is_ui_url,
    login_and_reach_dashboard,
)
from lightkube import Client, codecs
from utils import PROFILE_RESOURCE, assert_namespace_active, assert_resource_deleted

log = logging.getLogger(__name__)

# Assets directory is relative to the repository root.
ASSETS_DIR = Path(__file__).parent.parent.parent.parent / "assets"
PROFILE_TEMPLATE_FILE = ASSETS_DIR / "test-profile.yaml.j2"

DOMAIN_AUTH = "auth.kubeflow.com"
DOMAIN_UI = "ui.kubeflow.com"

NAMESPACE = "test-ui-iam"


@pytest.fixture(scope="module")
def auth_domain(request: pytest.FixtureRequest) -> str:
    """IdP (auth) hostname."""
    return domain if (domain := request.config.getoption("--iam-domain-auth")) else DOMAIN_AUTH


@pytest.fixture(scope="module")
def ui_domain(request: pytest.FixtureRequest) -> str:
    """Kubeflow UI hostname."""
    return domain if (domain := request.config.getoption("--iam-domain-ui")) else DOMAIN_UI


@pytest.fixture(scope="module")
def ui_ip(lightkube_client, application_model, ui_domain):
    """LoadBalancer IP of the istio Gateway serving the Kubeflow UI."""
    return get_ui_lb_ip(lightkube_client, application_model, ui_domain)


@pytest.fixture(scope="module")
def browser(playwright, ui_ip, auth_ip, ui_domain, auth_domain):
    """Launch headless Chromium with host-resolver-rules pointing the in-cluster
    domains at their discovered LoadBalancer IPs (no ``/etc/hosts`` / root needed)."""
    rules = build_host_resolver_rules(ui_ip, auth_ip, ui_domain, auth_domain)
    args = ["--no-sandbox", "--host-resolver-rules=" + rules]
    browser = playwright.chromium.launch(headless=True, args=args)
    yield browser
    browser.close()


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


def test_unauthenticated_request_is_redirected_to_login(context, ui_domain, auth_domain):
    """An unauthenticated request to the UI is redirected to the IdP login page."""
    page = context.pages[0]
    goto_login_form(page, ui_url=f"https://{ui_domain}", auth_domain=auth_domain)
    page.get_by_role("heading", name="Sign in").wait_for(state="visible", timeout=60_000)

    assert is_auth_url(page.url, auth_domain), f"Expected to land on {auth_domain}, got {page.url}"
    log.info("✓ Unauthenticated request was redirected to the IdP login page.")


def test_login_reaches_dashboard(context, kratos_user, create_profile, ui_domain):
    """A valid IdP login reaches the Kubeflow central dashboard."""
    _, email, password, _, _ = kratos_user

    page = context.pages[0]
    login_and_reach_dashboard(page, email, password, profile_namespace=NAMESPACE)

    assert is_ui_url(
        page.url, ui_domain
    ), f"Expected dashboard on host {ui_domain}, got {page.url}"
    log.info("✓ Login reached the Kubeflow dashboard.")
