# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Helpers for the COS (Canonical Observability Stack) integration tests.

These helpers discover the endpoints of COS Lite from the Juju model it is deployed to, and query
Prometheus, Loki and Grafana through Traefik (so that only its LoadBalancer has to be reachable
from where the tests run) to assert that the telemetry of the applications under test reached COS.
"""

import json
import logging
import time
from pathlib import Path

import jubilant
import requests
import tenacity
import urllib3
import yaml

log = logging.getLogger(__name__)

# Disable the noisy warnings emitted when talking to endpoints with self-signed certificates.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Assets directory is relative to the repository root.
ASSETS_DIR = Path(__file__).parent.parent.parent / "assets"

# COS Lite applications, as named by its Terraform module:
GRAFANA_APP = "grafana"
LOKI_APP = "loki"
PROMETHEUS_APP = "prometheus"
TRAEFIK_APP = "traefik"
GRAFANA_ADMIN_USER = "admin"

# The collector forwarding the telemetry of the application model to COS, and the endpoints
# integrating it with COS:
COLLECTOR_APP = "opentelemetry-collector-k8s"
COLLECTOR_COS_ENDPOINTS = ("send-remote-write", "send-loki-logs", "grafana-dashboards-provider")

ACTIVE_TIMEOUT = 60 * 10
REQUEST_TIMEOUT = 30
LOGS_LOOKBACK_HOURS = 24

# Telemetry, alert rules and dashboards reach COS asynchronously (on the collector's scrape and
# forwarding intervals), so the assertions on them are retried:
retry_for_five_minutes = tenacity.retry(
    wait=tenacity.wait_fixed(15),
    stop=tenacity.stop_after_delay(60 * 5),
    before_sleep=tenacity.before_sleep_log(log, logging.INFO),
    reraise=True,
)


def load_expected_telemetry(path: Path) -> dict[str, dict]:
    """Return the telemetry expected in COS, by application, as listed in the given file."""
    return yaml.safe_load(path.read_text())["applications"]


def apps_expecting(expected_telemetry: dict[str, dict], signal: str) -> list[str]:
    """Return the applications expected to send the given signal to COS."""
    return [app for app, signals in expected_telemetry.items() if signals.get(signal)]


def leader_unit(juju: jubilant.Juju, app: str) -> str:
    """Return the name of the leader unit of an application."""
    for name, unit in juju.status().apps[app].units.items():
        if unit.leader:
            return name
    raise AssertionError(f"No leader unit found for application '{app}'")


def wait_for_active(juju: jubilant.Juju, *apps: str, timeout: float = ACTIVE_TIMEOUT) -> None:
    """Wait for the given applications (all the model's if none are given) to be active."""
    juju.wait(
        lambda status: jubilant.all_active(status, *apps),
        error=lambda status: jubilant.any_error(status, *apps),
        timeout=timeout,
        delay=10,
    )


def assert_collector_integrations(status: jubilant.Status) -> None:
    """Assert the collector is deployed and integrated with COS.

    Args:
        status: The status of the Juju model where the collector is deployed.
    """
    assert (
        COLLECTOR_APP in status.apps
    ), f"{COLLECTOR_APP} is not deployed in model '{status.model.name}'"
    relations = status.apps[COLLECTOR_APP].relations

    unrelated = [endpoint for endpoint in COLLECTOR_COS_ENDPOINTS if not relations.get(endpoint)]
    assert not unrelated, f"{COLLECTOR_APP} is not related to COS over {unrelated}"


def get_proxied_endpoints(cos_juju: jubilant.Juju) -> dict[str, dict]:
    """Return the endpoints proxied by Traefik in the COS model, by application or unit name."""
    task = cos_juju.run(leader_unit(cos_juju, TRAEFIK_APP), "show-proxied-endpoints")
    proxied_endpoints = json.loads(task.results["proxied-endpoints"])
    assert TRAEFIK_APP in proxied_endpoints, "Traefik has no external address yet"
    log.info(f"Discovered the endpoints proxied by Traefik: {proxied_endpoints}")
    return proxied_endpoints


def get_cos_app_url(proxied_endpoints: dict[str, dict], cos_model: str, app: str) -> str:
    """Return the URL Traefik serves a COS application on.

    The applications ingressed per unit or per application (e.g. Prometheus and Loki) are listed
    in the endpoints proxied by Traefik, by unit or application name respectively. Those ingressed
    through a Traefik route instead (e.g. Grafana in COS Lite 2) are not, but are still served
    under the ``<model>-<application>`` path of Traefik's URL.
    """
    for name in sorted(proxied_endpoints):
        if name == app or name.startswith(f"{app}/"):
            return proxied_endpoints[name]["url"].rstrip("/")
    traefik_url = proxied_endpoints[TRAEFIK_APP]["url"].rstrip("/")
    # the model name may be prefixed with the controller name, which is not part of the path:
    return f"{traefik_url}/{cos_model.rpartition(':')[-1]}-{app}"


def get_grafana_admin_password(cos_juju: jubilant.Juju) -> str:
    """Return the password of the Grafana admin user, as generated by the Grafana charm."""
    task = cos_juju.run(leader_unit(cos_juju, GRAFANA_APP), "get-admin-password")
    return task.results["admin-password"]


def _get_json(url: str, **kwargs) -> dict | list:
    """Send a GET request, raising on HTTP errors, and return the JSON body of the response."""
    response = requests.get(url, timeout=REQUEST_TIMEOUT, verify=False, **kwargs)
    response.raise_for_status()
    return response.json()


def _topology_label_matchers(model: str, app: str) -> str:
    """Return the label matchers selecting the telemetry of an application by its Juju topology.

    Used in both Prometheus (PromQL) and Loki (LogQL) queries, which share the matcher syntax.
    """
    return f'juju_model="{model}",juju_application="{app}"'


def query_prometheus(prometheus_url: str, query: str) -> list[dict]:
    """Run an instant query on Prometheus and return the resulting samples."""
    body = _get_json(f"{prometheus_url}/api/v1/query", params={"query": query})
    return body["data"]["result"]


def get_prometheus_alert_rules(prometheus_url: str) -> list[dict]:
    """Return the alert rules loaded by Prometheus."""
    body = _get_json(f"{prometheus_url}/api/v1/rules", params={"type": "alert"})
    return [rule for group in body["data"]["groups"] for rule in group["rules"]]


def search_grafana_dashboards(grafana_url: str, password: str) -> list[dict]:
    """Return the dashboards provisioned in Grafana."""
    return _get_json(
        f"{grafana_url}/api/search",
        params={"type": "dash-db"},
        auth=(GRAFANA_ADMIN_USER, password),
    )


def query_loki(loki_url: str, query: str, lookback_hours: int = LOGS_LOOKBACK_HOURS) -> list[dict]:
    """Return the log streams matching a query in Loki, with their latest line, over a lookback."""
    end = time.time_ns()
    start = end - lookback_hours * 3600 * 10**9
    body = _get_json(
        f"{loki_url}/loki/api/v1/query_range",
        params={"query": query, "start": start, "end": end, "limit": 1},
    )
    return body["data"]["result"]


@retry_for_five_minutes
def assert_metrics_reach_prometheus(
    prometheus_url: str, model: str, app: str, metrics: list[str]
) -> None:
    """Assert Prometheus has samples of the given metrics from an application."""
    matchers = _topology_label_matchers(model, app)
    missing = [
        metric
        for metric in metrics
        if not query_prometheus(prometheus_url, f"{metric}{{{matchers}}}")
    ]
    assert not missing, f"Prometheus has no samples of the metrics {missing} from {model}/{app}"


@retry_for_five_minutes
def wait_for_metric_value(
    prometheus_url: str,
    model: str,
    app: str,
    metric: str,
    labels: dict[str, str],
    minimum: float = 0,
) -> float:
    """Wait for a metric of an application to have a value of at least the minimum, and return it.

    The metric is selected by the application's Juju topology and the given labels. Should several
    series match (e.g. one per unit), the highest of their values is considered.
    """
    matchers = ",".join(
        [
            _topology_label_matchers(model, app),
            *(f'{name}="{value}"' for name, value in labels.items()),
        ]
    )
    samples = query_prometheus(prometheus_url, f"{metric}{{{matchers}}}")
    assert samples, f"Prometheus has no samples of {metric}{{{matchers}}}"
    value = max(float(sample["value"][1]) for sample in samples)
    assert value >= minimum, f"{metric}{{{matchers}}} is {value:g}, expected at least {minimum:g}"
    return value


@retry_for_five_minutes
def assert_alert_rules_reach_prometheus(
    prometheus_url: str, model: str, app: str, alert_rules: list[str]
) -> None:
    """Assert Prometheus has loaded the given alert rules of an application.

    The alert rules of an application are told apart by the Juju topology labels the charm
    libraries add to them, which are preserved when forwarded by the collector.
    """
    loaded = {
        rule["name"]
        for rule in get_prometheus_alert_rules(prometheus_url)
        if rule.get("labels", {}).get("juju_model") == model
        and rule.get("labels", {}).get("juju_application") == app
    }
    missing = sorted(set(alert_rules) - loaded)
    assert not missing, (
        f"Prometheus has not loaded the alert rules {missing} of {model}/{app}"
        f" (loaded: {sorted(loaded)})"
    )


@retry_for_five_minutes
def assert_dashboards_reach_grafana(
    grafana_url: str, password: str, dashboards: list[str]
) -> None:
    """Assert Grafana has provisioned dashboards with the given titles."""
    titles = {dashboard["title"] for dashboard in search_grafana_dashboards(grafana_url, password)}
    missing = sorted(set(dashboards) - titles)
    assert not missing, f"Grafana has not provisioned the dashboards {missing}"


@retry_for_five_minutes
def assert_logs_reach_loki(loki_url: str, model: str, app: str) -> None:
    """Assert Loki has logs from an application over the lookback window."""
    streams = query_loki(loki_url, f"{{{_topology_label_matchers(model, app)}}}")
    assert streams, f"Loki has no logs from {model}/{app} in the last {LOGS_LOOKBACK_HOURS}h"
