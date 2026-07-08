# M2M Identity Integration Test

This suite validates **programmatic (machine-to-machine) access** to a KServe
`InferenceService` using a token issued by the Identity Platform (Hydra).

It obtains a JWT via the OAuth `client_credentials` grant and uses it to reach an
`InferenceService` through the Istio ingress gateway serving the KServe domain, from
**outside** the cluster.

The tests live in `driver/m2m/`, separate from the main UAT suite, and are skipped
unless `--include-m2m-tests` is passed.

## What it Tests

Given the deployment described under [Prerequisites](#prerequisites), the suite:

1. Discovers the istio `Gateway` serving the KServe domain (`api.kubeflow.com`) by
   matching its listener hostname — the charm/app name is **not** hardcoded — and
   patches its listeners to the wildcard hostname `*.api.kubeflow.com` (workaround
   for
   [canonical/service-mesh#102](https://github.com/canonical/service-mesh/issues/102)).
2. Creates a Kubeflow `Profile` (`test-m2m`).
3. Creates a KServe `InferenceService` (`sklearn-v2-iris`) in that namespace and
   waits for it to become `Ready`.
4. Creates an OAuth client in Hydra (`client_credentials` grant) and authorizes it
   as a contributor on the `Profile` (by creating the `RoleBinding` +
   `AuthorizationPolicy` that `github-profiles-automator` would otherwise create
   from a PMR `contributor` entry). The gateway's Istio principal is derived from
   the discovered `Gateway` (`<gateway>-istio`).
5. Obtains a token from Hydra and asserts the following:

| Test | Request | Expected |
| --- | --- | --- |
| `test_authorized_token_reaches_inferenceservice` | valid token from an authorized client | `200` with a prediction |
| `test_missing_token_is_rejected` | no `Authorization` header | `401` or `403` |
| `test_invalid_token_is_rejected` | invalid bearer token | `401` (rejected by `RequestAuthentication`) |
| `test_unauthorized_token_is_forbidden` | valid token from a client that is **not** a contributor | `403` (RBAC: access denied) |

All resources (Profile, InferenceService, RoleBinding, AuthorizationPolicy, OAuth
clients) are cleaned up at the end of the module.

## Prerequisites

- A Kubeflow + Identity Platform deployment with an ambient service mesh — e.g. the
  [`kubeflow-ambient-iam`](https://github.com/canonical/charmed-kubeflow-solutions/tree/feat/iam-integration/terraform-refactoring/tests/kubeflow-ambient-iam)
  test setup in `charmed-kubeflow-solutions` — which provides:
  - Juju models `iam` and `kubeflow`.
  - An istio ingress `Gateway` serving `api.kubeflow.com` (deployed via the
    `istio-ingress-k8s` charm; its app name is discovered at runtime).
  - Hydra (`iam` model) and `oauth2-proxy-k8s` (`kubeflow` model).
- DNS (or `/etc/hosts`) configured so the host can resolve the JWT issuer hostname
  returned by oauth2-proxy (e.g. `auth.kubeflow.com`).
- `juju` logged in to the controller (used via jubilant) and a valid `KUBECONFIG`
  pointing at the cluster (used via lightkube).
- Python dependencies installed (pytest, jubilant, lightkube, tenacity,
  requests-oauthlib).

> **Note on `github-profiles-automator`:** in the deployed setup this charm is the
> source of truth for Profile contributors and reconciles the cluster against its
> PMR repo. Because the test authorizes the client directly (the `test-m2m` Profile
> is not in the PMR), a reconcile during the run could remove that access. Run the
> suite on its own (`-k m2m`, which completes quickly) or pause the automator while
> testing.

## Running the Test

```bash
# Local mode (tests run from the host)
tox -e uats-local -- --include-m2m-tests -k m2m

# Remote mode (tests run from a cloned git repo)
tox -e uats-remote -- --include-m2m-tests -k m2m
```

By default (without `--include-m2m-tests`) these tests are skipped.

## Test Implementation Files

- `driver/m2m/test_m2m_inference.py` — test implementation and fixtures.
- `driver/m2m/helpers.py` — helpers for Juju actions, token retrieval, the gateway
  patch, contributor authorization and the inference request.
- `driver/m2m/conftest.py` — shares fixtures/utils with the main driver.
- `assets/kserve-inference-service.yaml.j2` — the `InferenceService` template.
