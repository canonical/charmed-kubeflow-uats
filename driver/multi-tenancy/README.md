# Multi-Tenancy Integration Test

This suite validates multi-tenancy in Charmed Kubeflow: a tenant configured with its own
object-storage integrator gets its own KFP artifact store, isolated from other tenants.

It is modelled on the ambient suite (`driver/ambient/`) and lives under `driver/` so it
reuses the shared `driver/utils.py` helpers. Unlike the rest of the driver, Juju
orchestration here uses **[jubilant](https://github.com/canonical/jubilant)** (a
synchronous wrapper over the `juju` CLI) via a module-scoped `juju` fixture bound to the
existing `kubeflow` model. See `multitenancy-test-suite-design.md` at the repo root for
the full design.

## Tenant model

Two tenants, each a Kubeflow Profile backed by a different S3 bucket:

| Tenant | Profile (namespace) | Object store |
|---|---|---|
| Override | `mt-tenant-override` | `S3_BUCKET_KFP_OVERRIDE` @ `S3_SERVER_URL_OVERRIDE`, served by the `data-kubeflow-integrator-override` deployed by this suite |
| Global | `mt-tenant-global` | `S3_BUCKET_KFP_GLOBAL` @ `S3_SERVER_URL_GLOBAL`, the default KFP artifact store |

The suite fully controls the **override** tenant. The **global** tenant represents the
default artifact store / another tenant, used to prove cross-tenant isolation.

## How it Works

### Phase 1 — infrastructure (`test_multi_tenancy_infrastructure_ready`)

1. Deploy an override `s3-integrator` (`s3-integrator-override`) configured from the
   `*_OVERRIDE` environment variables; credentials are supplied via a Juju user secret
   (`s3_secret_kfp_override`) referenced through the `credentials` config option.
2. Deploy a `data-kubeflow-integrator` (`data-kubeflow-integrator-override`) and integrate
   it with the override `s3-integrator` (over `kfp-s3-storage`) and with the existing
   `resource-dispatcher` (over the `secrets` and `config-maps` relations).
3. Deployment channels are detected from the already-deployed charms (the override
   `s3-integrator` reuses the channel of the existing `s3-integrator-global`), with
   per-charm fallbacks.
4. Assert the override `s3-integrator` and `resource-dispatcher` are `active`, and that the
   `data-kubeflow-integrator` is **blocked** — it stays blocked until its target Profile
   namespace exists, which is created later in Phase 2.

### Phase 2 — multi-tenancy assertions

- `test_tenant_kfp_resources_dispatched` — the per-tenant KFP resources
  (`mlpipeline-minio-artifact` Secret, `kfp-launcher` and `artifact-repositories`
  ConfigMaps) are dispatched into the tenant namespace and reference the override bucket.
- `test_tenant_pipeline_artifacts_isolated_to_own_bucket` — a pipeline run as the tenant
  stores artifacts in the override bucket, and not in the global bucket.
- `test_cross_tenant_bucket_isolation` — the two tenants use distinct object stores.
- `test_cross_tenant_kfp_api_denied` — the tenant identity cannot list another tenant's
  experiments via the KFP API (HTTP 403 Forbidden).
- `test_tenant_pipeline_run_succeeds` — the tenant can run a pipeline end-to-end.
- `test_cross_tenant_kfp_api_denied_within_mesh` — from a pod inside the tenant
  namespace, spoofing another tenant's `kubeflow-userid` is blocked by the Istio Ambient
  Mesh (HTTP 403 / `RBAC: access denied`). Skipped unless the ambient mesh is deployed.

The Phase 2 tests log their key steps (bucket snapshots, experiment/run creation, run
completion state, cross-tenant denial code) at INFO for easier triage.

## Layout

```text
driver/multi-tenancy/
  conftest.py                        # adds driver/ to sys.path to reuse driver/utils.py
  test_multi_tenancy_integration.py  # fixtures + Phase 1 & Phase 2 tests
  s3.py                              # S3BucketWrapper vendored from kfp-operators kfp-api
  assets/
    data_passing_pipeline.yaml       # compiled KFP v2 data-passing pipeline (caching off)
  README.md
```

## Prerequisites

- A deployed Kubeflow cluster with an existing `resource-dispatcher` and an
  `s3-integrator` named `s3-integrator-global` (the global KFP object store; the bundle
  backs KFP with `s3-integrator` rather than MinIO).
- A merged/released [data-kubeflow-integrator PR #34](https://github.com/canonical/data-kubeflow-integrator/pull/34)
  on the deployed channel (provides the `kfp-s3-storage` and `config-maps` endpoints).
- `kubectl` and Juju CLI configured for the cluster.
- The following environment variables (directly, or via a git-ignored `.env` file at the
  repo root, loaded with `python-dotenv`):
  - `S3_ACCESS_KEY_OVERRIDE`, `S3_SECRET_KEY_OVERRIDE`, `S3_SERVER_URL_OVERRIDE`,
    `S3_BUCKET_KFP_OVERRIDE` (required)
  - `S3_ACCESS_KEY_GLOBAL`, `S3_SECRET_KEY_GLOBAL`, `S3_SERVER_URL_GLOBAL`,
    `S3_BUCKET_KFP_GLOBAL` (required by the bucket-isolation tests)

## Running

```bash
# Local mode
tox -e uats-local -- --include-multi-tenancy-tests -k multi_tenancy

# Remote mode
tox -e uats-remote -- --include-multi-tenancy-tests -k multi_tenancy
```

By default these tests are skipped unless `--include-multi-tenancy-tests` is passed.

## Notes

- Teardown removes everything the suite created: the `data-kubeflow-integrator-override`
  and `s3-integrator-override` applications (and their relations), the
  `s3_secret_kfp_override` user secret, and any tenant Profiles/Pods created during the
  run.
- The suite assumes a clean model (these override applications and the secret do not
  already exist) and never removes/reconfigures the pre-existing global `s3-integrator`,
  `resource-dispatcher`, or any pre-existing `data-kubeflow-integrator`.
- Ambient-mesh detection keys off the `istio-beacon-k8s` app in `juju.status()`; adjust
  the `_AMBIENT_MESH_APPS` constant if a different indicator charm is used.
- The exact key names inside the dispatched Secret/ConfigMaps depend on the PR #34
  templates; the assertions check membership to stay resilient to key naming.
