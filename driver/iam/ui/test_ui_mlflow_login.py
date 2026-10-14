# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""UI (Identity login) integration tests for standalone MLflow.

These tests log in to the MLflow UI through the Canonical Identity Platform (IdP) using a
headless Chromium browser, and assert the MLflow app loads. They assume a standalone MLflow +
Identity Platform deployment with an ambient service mesh (the ``mlflow-ambient-iam`` setup in
charmed-kubeflow-solutions): Juju models ``iam``, ``iam-core`` and ``mlflow``, a Kratos
identity, the ``identity-platform-login-ui`` behind a ``traefik-lb`` Service, and an istio
ingress gateway serving ``ui.mlflow.com``.

The login/browser fixtures (``playwright``, ``context``, ``auth_ip``, ``iam_juju``,
``kratos_user``) are shared with the Kubeflow UI suite via ``iam/ui/conftest.py``; only the UI
LoadBalancer IP and the browser (which depend on the ``mlflow.com`` domains) are defined here.

Unlike the Kubeflow suite these tests create no Profile and need no MLflow workspace grant:
they exercise only the authentication path (reaching the MLflow UI), which any authenticated
user can do — workspace RBAC only gates the content APIs the SPA calls, not the login flow.
"""

import logging

import pytest
from iam.ui.helpers import (
    build_host_resolver_rules,
    get_ui_lb_ip,
    goto_login_form,
    is_auth_url,
    is_ui_url,
    login_and_reach_mlflow_ui,
)

log = logging.getLogger(__name__)

DOMAIN_AUTH = "auth.mlflow.com"
DOMAIN_UI = "ui.mlflow.com"


@pytest.fixture(scope="module")
def auth_domain(request: pytest.FixtureRequest) -> str:
    """IdP (auth) hostname."""
    return domain if (domain := request.config.getoption("--iam-domain-auth")) else DOMAIN_AUTH


@pytest.fixture(scope="module")
def ui_domain(request: pytest.FixtureRequest) -> str:
    """MLflow UI hostname."""
    return domain if (domain := request.config.getoption("--iam-domain-ui")) else DOMAIN_UI


@pytest.fixture(scope="module")
def ui_ip(lightkube_client, application_model, ui_domain):
    """LoadBalancer IP of the istio Gateway serving the MLflow UI."""
    return get_ui_lb_ip(lightkube_client, application_model, ui_domain)


@pytest.fixture(scope="module")
def browser(playwright, ui_ip, auth_ip, ui_domain, auth_domain):
    """Launch headless Chromium with host-resolver-rules pointing the MLflow UI and auth
    domains at their discovered LoadBalancer IPs (no ``/etc/hosts`` / root needed)."""
    rules = build_host_resolver_rules(ui_ip, auth_ip, ui_domain, auth_domain)
    args = ["--no-sandbox", "--host-resolver-rules=" + rules]
    browser = playwright.chromium.launch(headless=True, args=args)
    yield browser
    browser.close()


def test_unauthenticated_request_is_redirected_to_login(context, ui_domain, auth_domain):
    """An unauthenticated request to the MLflow UI is redirected to the IdP login page."""
    page = context.pages[0]
    goto_login_form(page, ui_url=f"https://{ui_domain}", auth_domain=auth_domain)
    page.get_by_role("heading", name="Sign in").wait_for(state="visible", timeout=60_000)

    assert is_auth_url(page.url, auth_domain), (
        f"Expected to land on {auth_domain}, got {page.url}"
    )
    log.info("✓ Unauthenticated request was redirected to the IdP login page.")


def test_login_reaches_mlflow_ui(context, kratos_user, ui_domain, auth_domain):
    """A valid IdP login reaches the MLflow UI.

    No MLflow workspace grant is needed: landing back on the UI host proves authentication
    succeeded, and the tracking server serves the MLflow app to any authenticated user.
    """
    _, email, password, _, _ = kratos_user

    page = context.pages[0]
    login_and_reach_mlflow_ui(page, email, password, ui_domain, auth_domain)

    assert is_ui_url(page.url, ui_domain), (
        f"Expected the MLflow UI on host {ui_domain}, got {page.url}"
    )
    log.info("✓ Login reached the MLflow UI.")
