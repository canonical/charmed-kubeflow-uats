# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""COS (observability) integration tests for standalone MLflow.

These tests validate that the telemetry of standalone MLflow reaches COS Lite through the
``opentelemetry-collector-k8s`` deployed alongside it: the metrics and alert rules of its charms
in Prometheus, their dashboards in Grafana and their logs in Loki.

They assume a standalone MLflow deployment with COS Lite (see the ``mlflow-ambient-iam-cos`` setup
in charmed-kubeflow-solutions): Juju models ``mlflow`` and ``cos``, with
opentelemetry-collector-k8s related to mlflow-server on the ``mlflow`` model, and the COS Lite
(Grafana, Loki, Prometheus) on the ``cos`` model.

The telemetry expected from each application is listed in ``assets/cos-mlflow.yaml``.
"""

import logging

import jubilant
import pytest

from cos.helpers import (
    ASSETS_DIR,
    COLLECTOR_APP,
    apps_expecting,
    assert_alert_rules_reach_prometheus,
    assert_collector_integrations,
    assert_dashboards_reach_grafana,
    assert_logs_reach_loki,
    assert_metrics_reach_prometheus,
    load_expected_telemetry,
    wait_for_active,
)

log = logging.getLogger(__name__)

EXPECTED_TELEMETRY = load_expected_telemetry(ASSETS_DIR / "cos-mlflow.yaml")


@pytest.fixture(scope="module")
def mlflow_juju(application_model: str) -> jubilant.Juju:
    """A Jubilant handle to the MLflow model."""
    return jubilant.Juju(model=application_model)


def test_cos_is_active(cos_juju: jubilant.Juju, mlflow_juju: jubilant.Juju):
    """COS and the collector forwarding MLflow's telemetry to it are integrated and active."""
    assert_collector_integrations(mlflow_juju.status())
    wait_for_active(mlflow_juju, COLLECTOR_APP)
    wait_for_active(cos_juju)
    log.info("✓ COS and the collector are integrated and active.")


@pytest.mark.parametrize("app", apps_expecting(EXPECTED_TELEMETRY, "metrics"))
def test_metrics_reach_prometheus(app: str, prometheus_url: str, application_model: str):
    """The metrics exported by the application reach Prometheus."""
    assert_metrics_reach_prometheus(
        prometheus_url, application_model, app, EXPECTED_TELEMETRY[app]["metrics"]
    )
    log.info(f"✓ The metrics of {app} reached Prometheus.")


@pytest.mark.parametrize("app", apps_expecting(EXPECTED_TELEMETRY, "alert_rules"))
def test_alert_rules_reach_prometheus(app: str, prometheus_url: str, application_model: str):
    """The alert rules bundled with the application's charm are loaded by Prometheus."""
    assert_alert_rules_reach_prometheus(
        prometheus_url, application_model, app, EXPECTED_TELEMETRY[app]["alert_rules"]
    )
    log.info(f"✓ The alert rules of {app} reached Prometheus.")


@pytest.mark.parametrize("app", apps_expecting(EXPECTED_TELEMETRY, "dashboards"))
def test_dashboards_reach_grafana(app: str, grafana_url: str, grafana_admin_password: str):
    """The dashboards bundled with the application's charm are provisioned in Grafana."""
    assert_dashboards_reach_grafana(
        grafana_url, grafana_admin_password, EXPECTED_TELEMETRY[app]["dashboards"]
    )
    log.info(f"✓ The dashboards of {app} reached Grafana.")


@pytest.mark.parametrize("app", apps_expecting(EXPECTED_TELEMETRY, "logs"))
def test_logs_reach_loki(app: str, loki_url: str, application_model: str):
    """The logs of the application reach Loki."""
    assert_logs_reach_loki(loki_url, application_model, app)
    log.info(f"✓ The logs of {app} reached Loki.")
