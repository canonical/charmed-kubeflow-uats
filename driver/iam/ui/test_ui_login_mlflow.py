# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""UI (Identity login) integration tests for MLflow standalone.

These tests log in to the MLflow UI through the Canonical Identity Platform (IdP) using a
headless Chromium browser, and assert the MLflow app loads. They assume a standalone MLflow +
Identity Platform deployment with an ambient service mesh (the ``mlflow-ambient-iam`` setup in
charmed-kubeflow-solutions): Juju models ``iam``, ``iam-core`` and ``mlflow``, a Kratos
identity, the ``identity-platform-login-ui`` behind a ``traefik-lb`` Service, and an istio
ingress gateway serving ``ui.mlflow.com``.

Unlike the Kubeflow suite these tests create no Profile and need no MLflow workspace grant:
they exercise only the authentication path (reaching the MLflow UI), which any authenticated
user can do — workspace RBAC only gates the content APIs the SPA calls, not the login flow.
"""

import logging

import pytest

from iam.ui.helpers import (
    MLFLOW_UI_PATH,
    assert_login_reaches_application_ui,
    assert_unauthenticated_request_is_redirected_to_login,
)

log = logging.getLogger(__name__)

# default domains for external access:
MLFLOW_DEFAULT_DOMAIN_AUTH = "auth.mlflow.com"
MLFLOW_DEFAULT_DOMAIN_UI = "ui.mlflow.com"


@pytest.fixture(scope="module")
def auth_domain(request: pytest.FixtureRequest) -> str:
    """IdP (auth) hostname."""
    return (
        domain
        if (domain := request.config.getoption("--iam-domain-auth"))
        else MLFLOW_DEFAULT_DOMAIN_AUTH
    )


@pytest.fixture(scope="module")
def ui_domain(request: pytest.FixtureRequest) -> str:
    """Application UI hostname."""
    return (
        domain
        if (domain := request.config.getoption("--iam-domain-ui"))
        else MLFLOW_DEFAULT_DOMAIN_UI
    )


def test_unauthenticated_request_is_redirected_to_login(context, ui_domain, auth_domain):
    """An unauthenticated request to the application UI is redirected to the IdP login page."""
    assert_unauthenticated_request_is_redirected_to_login(
        context, ui_domain, auth_domain, ui_path=MLFLOW_UI_PATH
    )


def test_login_reaches_application_ui(context, kratos_user, auth_domain, ui_domain):
    """A valid IdP login reaches the application UI.

    For Kubeflow, a Profile owned by the testing Kratos user is precreated.

    For MLflow standalone, no MLflow workspace grant is needed: landing back on the UI host proves
    authentication succeeded, and the tracking server serves the MLflow app to any authenticated
    user.
    """
    assert_login_reaches_application_ui(
        context,
        kratos_user,
        ui_domain,
        auth_domain,
        is_mlflow_standalone=True,
        kubeflow_profile_name=None,
    )
