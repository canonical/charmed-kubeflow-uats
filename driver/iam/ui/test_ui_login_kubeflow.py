# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""UI (Identity login) integration tests for Kubeflow.

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
from lightkube import Client, codecs

from iam.ui.helpers import (
    assert_login_reaches_application_ui,
    assert_unauthenticated_request_is_redirected_to_login,
)
from utils import PROFILE_RESOURCE, assert_namespace_active, assert_resource_deleted

log = logging.getLogger(__name__)

# for creating the testing Kubeflow Profile:
ASSETS_DIR = Path(__file__).parent.parent.parent.parent / "assets"  # relative to the repo root
PROFILE_TEMPLATE_FILE = ASSETS_DIR / "test-profile.yaml.j2"
NAMESPACE = "test-ui-iam"

# default domains for external access:
KUBEFLOW_DEFAULT_DOMAIN_AUTH = "auth.kubeflow.com"
KUBEFLOW_DEFAULT_DOMAIN_UI = "ui.kubeflow.com"


@pytest.fixture(scope="module")
def auth_domain(request: pytest.FixtureRequest) -> str:
    """IdP (auth) hostname."""
    return (
        domain
        if (domain := request.config.getoption("--iam-domain-auth"))
        else KUBEFLOW_DEFAULT_DOMAIN_AUTH
    )


@pytest.fixture(scope="module")
def ui_domain(request: pytest.FixtureRequest) -> str:
    """Application UI hostname."""
    return (
        domain
        if (domain := request.config.getoption("--iam-domain-ui"))
        else KUBEFLOW_DEFAULT_DOMAIN_UI
    )


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
    """An unauthenticated request to the application UI is redirected to the IdP login page."""
    assert_unauthenticated_request_is_redirected_to_login(context, ui_domain, auth_domain)


def test_login_reaches_application_ui(
    context, kratos_user, create_profile, auth_domain, ui_domain
):
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
        is_mlflow_standalone=False,
        kubeflow_profile_name=NAMESPACE,
    )
