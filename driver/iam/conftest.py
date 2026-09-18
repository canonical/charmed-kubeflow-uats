# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared fixtures for the IAM integration test suites (``m2m`` and ``ui``)."""

import logging

import jubilant
import pytest

log = logging.getLogger(__name__)

KUBEFLOW_MODEL = "kubeflow"

# Interval the kubeflow model's update-status hook is throttled to during the test run
# to stop github-profiles-automator from reconciling mid-test.
UPDATE_STATUS_HOOK_INTERVAL = "2h"


@pytest.fixture(scope="module", autouse=True)
def slow_update_status_hook():
    """Raise the ``kubeflow`` model's update-status interval for the test run.

    ``github-profiles-automator`` runs a full reconcile on every ``update_status``
    hook and revokes access to Profiles absent from its PMR, which deletes the
    RoleBinding/AuthorizationPolicy backing the IAM tests' Profiles and makes them
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
