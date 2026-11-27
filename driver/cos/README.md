# COS Integration Test

This suite validates that the **telemetry** of the application under test reaches the
[Canonical Observability Stack](https://charmhub.io/topics/canonical-observability-stack)
(COS Lite), as forwarded by the `opentelemetry-collector-k8s` deployed alongside it: its metrics
and alert rules in Prometheus, its dashboards in Grafana and its logs in Loki.

It borrows its approach from the observability test of `canonical/kserve-operators`
(`tests/integration/test_observability.py`): COS is reached through Traefik's proxied endpoints,
and the checks target the specific signals the charms ship (metrics the application itself
exports rather than `up`, which is reported even when scrapes fail, and the exact names of the
alert rules and titles of the dashboards bundled with the charms), rather than just the presence
of some telemetry.

The tests live in `driver/cos/`, separate from the main UAT suite, and are skipped unless
`--include-cos-tests` is passed. The test creating an MLflow experiment (see below) relies on the
setup of the [IAM M2M tests](../iam/m2m/README.md), so it also needs `--include-iam-m2m-tests`.
Only standalone MLflow is covered so far.

## What it Tests

Given the deployment described under [Prerequisites](#prerequisites), the suite:

1. Reads the telemetry expected from each application from `assets/cos-mlflow.yaml` (see
   [Expected telemetry](#expected-telemetry)).
2. Discovers the URLs of Prometheus, Loki and Grafana from Traefik's `show-proxied-endpoints`
   action in the `cos` model, and the Grafana admin password from Grafana's `get-admin-password`
   action.
3. Asserts the following, polling for up to 5 minutes as telemetry is ingested asynchronously:

| Test | Check | Expected |
| --- | --- | --- |
| `test_cos_is_active` | `opentelemetry-collector-k8s` in the `mlflow` model and the `cos` model | the collector is related to COS (`send-remote-write`, `send-loki-logs`, `grafana-dashboards-provider`); the collector and all of COS are active |
| `test_metrics_reach_prometheus[<app>]` | Prometheus `/api/v1/query` for each expected metric, filtered by the app's Juju topology (`juju_model`, `juju_application`) | at least one sample of each |
| `test_alert_rules_reach_prometheus[<app>]` | Prometheus `/api/v1/rules`, filtered by the app's Juju topology labels | every expected alert rule loaded |
| `test_dashboards_reach_grafana[<app>]` | Grafana `/api/search` | a dashboard with every expected title |
| `test_logs_reach_loki[<app>]` | Loki `/loki/api/v1/query_range` for the app's Juju topology, over the last 24h | at least one log line |
| `test_new_experiment_updates_workspace_metric` | `mlflow_workspace_metric{metric_name="num_experiments"}` of the `--mlflow-workspace` workspace (default: `test-workspace`), before and after creating an experiment in it through the M2M gateway | the count increases |

To create the experiment, `test_new_experiment_updates_workspace_metric` reuses the helpers of the
IAM M2M tests: it creates an OAuth client in Hydra, grants it admin on the workspace through a
throwaway `data-integrator` related to `mlflow-server:mlflow-client`, and creates the experiment
with a token of the client. All of them are removed at the end of the test (unless
`--keep-artifacts` is passed); the rest of the suite is read-only.

## Expected telemetry

`assets/cos-mlflow.yaml` lists, for each application, the telemetry it is expected to send to
COS: the metrics it exports, the names of the alert rules and the titles of the dashboards bundled
with its charm (under `src/prometheus_alert_rules/` and `src/grafana_dashboards/` respectively,
e.g. in [canonical/mlflow-operator](https://github.com/canonical/mlflow-operator)), and whether
its logs are forwarded. Keep it in sync with the charms under test.

## Prerequisites

- A standalone MLflow deployment with COS Lite — e.g. the
  [`mlflow-ambient-iam-cos`](https://github.com/canonical/charmed-kubeflow-solutions/tree/main/terraform/tests/mlflow-ambient-iam-cos)
  test setup in `charmed-kubeflow-solutions` — which provides:
  - Juju models `mlflow` and `cos` (their names can be passed via `--model` and `--cos-model`).
  - COS Lite in the `cos` model: Prometheus, Loki, Grafana, Alertmanager, Catalogue and Traefik.
  - `opentelemetry-collector-k8s` in the `mlflow` model, related to `mlflow-server`
    (`metrics-endpoint`, `logging` and `grafana-dashboard`) and to the COS Lite offers for
    Grafana dashboards, Loki logging and Prometheus remote-write.
- Traefik's LoadBalancer in the `cos` model reachable from the UAT host. No DNS configuration is
  required: the proxied endpoints are addressed by the LoadBalancer's address.
- `juju` logged in to the controller (used via jubilant).
- For `test_new_experiment_updates_workspace_metric` only, the setup of the
  [IAM M2M tests](../iam/m2m/README.md) for MLflow: the Identity Platform (Hydra) in the `iam`
  model, and `oauth2-proxy` and an istio ingress Gateway serving `api.mlflow.com` (or the domain
  passed via `--iam-domain-m2m`) in the `mlflow` model, with the JWT issuer hostname (e.g.
  `auth.mlflow.com`) resolvable from the UAT host, and a valid `KUBECONFIG` (used via lightkube to
  discover the Gateway). The workspace should already exist, as the exporter only gets read access
  to a new workspace on the next hook of the `mlflow-server` charm. On a fresh deployment, being
  run before the IAM tests, this test may also have to wait for the M2M gateway's JWT warm-up (up
  to 20 minutes).

## Running the Test

```bash
# Local mode (tests run from the host)
tox -e mlflow-local -- --model mlflow --mlflow-standalone --mlflow-tracking-uri http://mlflow-server.mlflow.svc.cluster.local:5000 --include-cos-tests -k cos

# Remote mode (tests run from a cloned git repo)
tox -e mlflow-remote -- --model mlflow --mlflow-standalone --mlflow-tracking-uri http://mlflow-server.mlflow.svc.cluster.local:5000 --include-cos-tests -k cos

# Including the test creating an MLflow experiment, along with the IAM M2M tests
tox -e mlflow-remote -- --model mlflow --mlflow-standalone --mlflow-tracking-uri http://mlflow-server.mlflow.svc.cluster.local:5000 --include-cos-tests --include-iam-m2m-tests
```

By default (without `--include-cos-tests`) these tests are skipped, and the MLflow ones are also
skipped without `--mlflow-standalone`. `test_new_experiment_updates_workspace_metric` is also
skipped without `--include-iam-m2m-tests`. If COS is deployed to a model other than `cos`, pass
its name via `--cos-model`.

## Test Implementation Files

- `driver/cos/test_cos_mlflow.py` — the tests for standalone MLflow.
- `driver/cos/helpers.py` — helpers for discovering the COS endpoints, querying Prometheus, Loki
  and Grafana, and the (retried) assertions on the telemetry.
- `driver/cos/conftest.py` — fixtures shared by the COS suites (COS model, endpoints, Grafana
  credentials).
- `assets/cos-mlflow.yaml` — the telemetry expected from the applications of standalone MLflow.
- `driver/iam/m2m/helpers.py` and `driver/ingress.py` — the helpers of the IAM M2M tests, reused
  to create the MLflow experiment through the M2M gateway.
