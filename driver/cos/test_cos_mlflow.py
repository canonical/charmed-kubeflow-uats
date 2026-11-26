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

The last test also creates an experiment through MLflow's M2M gateway, with a token issued by the
Identity Platform as in the M2M tests, so it relies on the same setup and only runs if the IAM M2M
tests are included too.
"""

import json
import logging
from uuid import uuid4

import jubilant
import pytest
from lightkube import Client

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
    wait_for_metric_value,
)
from iam.m2m.helpers import (
    IAM_MODEL,
    MLFLOW_DEFAULT_DOMAIN_M2M,
    MLFLOW_SERVER_APP,
    create_oauth_client,
    delete_oauth_client,
    get_jwt_issuer_url,
    get_token,
    grant_mlflow_workspace,
    mlflow_create_experiment_with_jwt_warmup,
    mlflow_delete_experiment,
)
from ingress import find_gateway_for_domain, gateway_service_name, get_service_lb_ip

log = logging.getLogger(__name__)

EXPECTED_TELEMETRY = load_expected_telemetry(ASSETS_DIR / "cos-mlflow.yaml")

# For the test creating an experiment through the M2M gateway: the throwaway data-integrator
# granting the test's OAuth client access to the workspace (named apart from the M2M tests' one so
# that they never clash), and the metric expected to count the new experiment:
GRANT_APP = "uat-cos-grant"
WORKSPACE_METRIC = "mlflow_workspace_metric"


@pytest.fixture(scope="module")
def mlflow_juju(application_model: str) -> jubilant.Juju:
    """A Jubilant handle to the MLflow model."""
    return jubilant.Juju(model=application_model)


@pytest.fixture(scope="module")
def mlflow_workspace(request: pytest.FixtureRequest) -> str:
    """The MLflow workspace the MLflow tests target."""
    return request.config.getoption("--mlflow-workspace")


@pytest.fixture(scope="module")
def m2m_domain(request: pytest.FixtureRequest) -> str:
    """MLflow's M2M hostname."""
    return request.config.getoption("--iam-domain-m2m") or MLFLOW_DEFAULT_DOMAIN_M2M


@pytest.fixture(scope="module")
def gateway_ip(lightkube_client: Client, application_model: str, m2m_domain: str) -> str:
    """LoadBalancer IP of the istio Gateway serving MLflow's M2M hostname."""
    gateway = find_gateway_for_domain(lightkube_client, application_model, m2m_domain)
    return get_service_lb_ip(lightkube_client, application_model, gateway_service_name(gateway))


@pytest.fixture(scope="module")
def oauth_client(keep_artifacts: bool):
    """An OAuth client for the ``client_credentials`` grant, created in Hydra."""
    client_id, client_secret = create_oauth_client(IAM_MODEL, "uat-cos-mlflow")

    yield client_id, client_secret

    if keep_artifacts:
        log.info(f"Keeping OAuth client {client_id} (--keep-artifacts set)")
        return
    delete_oauth_client(IAM_MODEL, client_id)


@pytest.fixture(scope="module")
def granted_token(
    oauth_client,
    mlflow_juju: jubilant.Juju,
    mlflow_workspace: str,
    application_model: str,
    keep_artifacts: bool,
) -> str:
    """A token of the OAuth client, granted admin on the workspace via a throwaway data-integrator.

    The client is granted access through the same sanctioned path a real programmatic consumer
    uses, as in the M2M tests (see ``grant_mlflow_workspace``), and the grant is removed on
    teardown.
    """
    client_id, client_secret = oauth_client
    token = get_token(client_id, client_secret, get_jwt_issuer_url(application_model))

    with grant_mlflow_workspace(
        mlflow_juju, GRANT_APP, client_id, mlflow_workspace, keep_artifacts
    ):
        yield token


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


@pytest.mark.iam_m2m
def test_new_experiment_updates_workspace_metric(
    prometheus_url: str,
    application_model: str,
    mlflow_workspace: str,
    m2m_domain: str,
    gateway_ip: str,
    granted_token: str,
    keep_artifacts: bool,
):
    """Creating an experiment in the workspace increases its experiment count in Prometheus.

    The count is polled from MLflow by the exporter, so this checks that the metrics reaching COS
    are kept up to date with MLflow's actual state.
    """
    labels = {"workspace": mlflow_workspace, "metric_name": "num_experiments"}
    before = wait_for_metric_value(
        prometheus_url, application_model, MLFLOW_SERVER_APP, WORKSPACE_METRIC, labels
    )
    log.info(f"Workspace '{mlflow_workspace}' has {before:g} experiments according to Prometheus")

    name = f"uat-cos-{uuid4().hex[:8]}"
    http_code, body = mlflow_create_experiment_with_jwt_warmup(
        m2m_domain, gateway_ip, granted_token, mlflow_workspace, name
    )
    assert (
        http_code == 200
    ), f"Expected HTTP 200 creating experiment {name}, got {http_code}: {body}"
    experiment_id = json.loads(body)["experiment_id"]

    try:
        after = wait_for_metric_value(
            prometheus_url,
            application_model,
            MLFLOW_SERVER_APP,
            WORKSPACE_METRIC,
            labels,
            minimum=before + 1,
        )
        log.info(f"✓ The experiment count of workspace '{mlflow_workspace}' rose to {after:g}.")
    finally:
        if keep_artifacts:
            log.info(f"Keeping experiment {name} (--keep-artifacts set)")
        else:
            _delete_experiment(
                m2m_domain, gateway_ip, granted_token, mlflow_workspace, experiment_id
            )


def _delete_experiment(
    m2m_domain: str, gateway_ip: str, token: str, workspace: str, experiment_id: str
) -> None:
    """Delete an experiment on a best-effort basis, never failing the test on it."""
    try:
        http_code, body = mlflow_delete_experiment(
            m2m_domain, gateway_ip, token, workspace, experiment_id
        )
    except Exception as error:
        log.warning(f"Failed to delete experiment {experiment_id}: {error}")
        return
    if http_code != 200:
        log.warning(f"Failed to delete experiment {experiment_id}: HTTP {http_code}: {body}")
