# UI Identity Login Integration Test

This suite validates **interactive (human) access** to the Kubeflow dashboard by
logging in through the Canonical Identity Platform (IdP) with a headless Chromium
browser, and asserting the central dashboard renders.

It borrows login-flow + browser-launch conventions from
`canonical/tenant-service` `tests/browser` (the Identity team's own specs):
`ignoreHTTPSErrors` and `--host-resolver-rules` at launch, identifier-first login
split into `enter_email` / `enter_password`, single worker, `retain-on-failure`
trace + video.

The tests live in `driver/ui/`, separate from the main UAT suite, and are skipped
unless `--include-ui-tests` is passed.

## What it Tests

Given the deployment described under [Prerequisites](#prerequisites), the suite:

1. Discovers at runtime the LoadBalancer IPs for the UI (istio Gateway serving
   `ui.kubeflow.com` in `kubeflow`) and the IdP (`traefik-lb` in `iam-core`), and
   hands both to Chromium via `--host-resolver-rules` so no `/etc/hosts` entry (and
   thus no root) is needed on the UAT host.
2. Creates a Kratos identity via the `create-admin-account` Juju action, passing the
   password through a Juju secret (`password-secret-id`) granted to `kratos`. The
   identity persists (no delete action exists); only the Juju secret is removed in
   teardown.
3. Creates a Kubeflow `Profile` (`test-ui-iam`) owned by the Kratos `username`
   directly via lightkube (bypassing `github-profiles-automator`/PMR).
4. Asserts:

| Test | Action | Expected |
| --- | --- | --- |
| `test_unauthenticated_request_is_redirected_to_login` | `GET https://ui.kubeflow.com` with no session | redirected to `kubeflow.com` (the IdP ingress) and the "Sign in" heading is visible |
| `test_login_reaches_dashboard` | fill email + password (single-page login form) | redirected back to `ui.kubeflow.com` and the central dashboard renders |

Profile-namespace visibility is **best-effort**: the suite attempts to confirm the
user's namespace is selectable in the dashboard; if that proves too brittle the
assertion is dropped (per design) and only URL host + dashboard-element are checked.

## Prerequisites

<!-- TODO: point this link at `main` once feat/iam-integration is merged. -->
- A Kubeflow + Identity Platform deployment with an ambient service mesh — e.g. the
  [`kubeflow-ambient-iam`](https://github.com/canonical/charmed-kubeflow-solutions/tree/feat/iam-integration/terraform-refactoring/tests/kubeflow-ambient-iam)
  test setup in `charmed-kubeflow-solutions` — which provides:
  - Juju models `iam`, `iam-core` and `kubeflow`.
  - A Kratos identity service (`iam` model).
  - The `identity-platform-login-ui` behind a `traefik-lb` Service in `iam-core`
    serving the apex domain `kubeflow.com` (both the oauth2-proxy auth endpoint and
    the login UI live under `kubeflow.com`, e.g. `kubeflow.com/ui/login`).
  - An istio ingress `Gateway` serving `ui.kubeflow.com` (deployed via the
    `istio-ingress-k8s` charm; its app name is discovered at runtime).
  - `oauth2-proxy` (`kubeflow` model) performing forward-auth against the IdP.
- **MFA disabled**: `kratos` must be deployed with `enforce_mfa=False`, otherwise a
  TOTP step breaks the headless login. This is a deployment-time step (out of scope
  for this repo — documented as a prerequisite for the solutions CI).
- `juju` logged in to the controller (used via jubilant) and a valid `KUBECONFIG`
  pointing at the cluster (used via lightkube).
- A Playwright Chromium browser installed on the runner before running the suite:
  ```bash
  poetry run playwright install --with-deps chromium
  ```
  (`--with-deps` needs passwordless sudo; the self-hosted runners used by the solutions
  CI have it.)
- No host DNS configuration is required: Chromium resolves the in-cluster domains to
  the discovered LoadBalancer IPs via `--host-resolver-rules`.

> **Note on `github-profiles-automator`:** the test creates the `test-ui-iam`
> Profile directly (it is not in the PMR), so a mid-run reconcile could remove it.
> The `charmed-kubeflow-solutions` CI that runs these UATs is expected to raise the
> charm's
> [`sync-period`](https://charmhub.io/github-profiles-automator/configurations#sync-period)
> to a large value beforehand so it does not reconcile mid-run. If you run this
> suite outside that CI, do the same first, e.g.
> `juju config -m kubeflow github-profiles-automator sync-period=86400`.

## Running the Test

```bash
# Local mode (tests run from the host)
tox -e uats-local -- --include-ui-tests -k ui

# Remote mode (tests run from a cloned git repo)
tox -e uats-remote -- --include-ui-tests -k ui
```

By default (without `--include-ui-tests`) these tests are skipped.

## Test Implementation Files

- `driver/ui/test_ui_login.py` — test implementation and fixtures.
- `driver/ui/helpers.py` — Kratos user/secret management, IP discovery,
  host-resolver-rules, identifier-first login flow, and dashboard-reached assertion.
- `driver/ui/conftest.py` — shares fixtures/utils with the main driver.
- `assets/ui-profile.yaml.j2` — the `Profile` template (owner = Kratos username).
