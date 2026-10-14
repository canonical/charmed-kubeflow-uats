# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Conftest for UI (Identity login) integration tests.

Holds the fixtures shared by every UI suite regardless of product (Kubeflow or standalone
MLflow): the Playwright instance, the per-test browser context (with failure artifacts), the
IdP (auth) LoadBalancer IP, the Jubilant handle to the IAM model, and the throwaway Kratos
user. Product-specific fixtures (the UI LoadBalancer IP and the browser, which depend on the
UI/auth domains) live in each test module.
"""

import logging
import sys
import time
from pathlib import Path

import jubilant
import pytest
from playwright.sync_api import sync_playwright

# Add this directory first (so `from helpers import` resolves to iam/ui/helpers.py,
# not iam/m2m/helpers.py when collecting the whole driver/ tree), then the driver
# directory to share fixtures/utils with the main driver.
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(1, str(Path(__file__).parent.parent.parent))

from iam.ui.helpers import (  # noqa: E402  (import must follow the sys.path setup above)
    IAM_MODEL,
    create_kratos_user,
    get_auth_lb_ip,
    remove_kratos_user,
)

log = logging.getLogger(__name__)

# Directory for failure artifacts (Playwright trace + screenshot). Uses a fixed path
# relative to the repo root so CI can reliably upload them.
ARTIFACTS_DIR = Path(__file__).parent.parent.parent.parent / "playwright-artifacts"


@pytest.fixture(scope="module")
def auth_ip(lightkube_client):
    """LoadBalancer IP of the IdP (auth) ingress."""
    return get_auth_lb_ip(lightkube_client)


@pytest.fixture(scope="module")
def playwright():
    """Start a Playwright instance for the module."""
    with sync_playwright() as p:
        yield p


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


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Expose the call-phase report on the item so fixtures can act on test outcome.

    Used by the ``context`` fixture to decide whether to retain the Playwright trace
    and screenshot (only on failure).
    """
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)
