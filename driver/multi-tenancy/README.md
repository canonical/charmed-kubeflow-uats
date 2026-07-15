# Multi-Tenancy Integration Test

This suite validates multi-tenancy in Charmed Kubeflow: a tenant configured with its own
object-storage integrator gets its own KFP artifact store, isolated from other tenants.

It is modelled on the ambient suite (`driver/ambient/`) and lives under `driver/` so it
reuses the shared `driver/utils.py` helpers. Unlike the rest of the driver, Juju
orchestration here uses **[jubilant](https://github.com/canonical/jubilant)** (a
synchronous wrapper over the `juju` CLI) via a module-scoped `juju` fixture bound to the
existing `kubeflow` model. See `multitenancy-test-suite-design.md` at the repo root for
the full design.

## What it Tests

Phase 1 (`test_multi_tenancy_infrastructure_ready`) deploys:

1. A second `s3-integrator` (`s3-integrator-override`) configured from the `*_OVERRIDE`
   environment variables, with credentials supplied through a Juju user secret.
2. A `data-kubeflow-integrator` (`data-kubeflow-integrator-override`) integrated with the
   override `s3-integrator` (over `kfp-s3-storage`) and with the existing
   `resource-dispatcher` (over the `secrets` and `config-maps` relations).

Phase 2 asserts multi-tenancy behaviour:

- `test_tenant_kfp_resources_dispatched` — the per-tenant KFP resources
  (`mlpipeline-minio-artifact` Secret, `kfp-launcher` and `artifact-repositories`
  ConfigMaps) are dispatched into the tenant namespace and reference the override bucket.
- `test_tenant_pipeline_artifacts_isolated_to_own_bucket` — a pipeline run as the tenant
  stores artifacts in the override bucket, and not in the global bucket.
- `test_cross_tenant_bucket_isolation` — the two tenants use distinct object stores.
- `test_cross_tenant_kfp_api_denied` — the tenant identity cannot list another tenant's
  experiments via the KFP API.
- `test_tenant_pipeline_run_succeeds` — the tenant can run a pipeline end-to-end.
- `test_cross_tenant_kfp_api_denied_within_mesh` — from a pod inside the tenant
  namespace, spoofing another tenant's `kubeflow-userid` is blocked by the Istio Ambient
  Mesh (HTTP 403 / `RBAC: access denied`). Skipped unless the ambient mesh is deployed.

## Prerequisites

- A deployed Kubeflow cluster with an existing `resource-dispatcher` and an
  `s3-integrator` named `s3-integrator-kfp`.
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

- No teardown of the deployed applications: `s3-integrator-override` and
  `data-kubeflow-integrator-override` (and the `s3_secret_kfp_override` secret) are left
  running after the suite finishes. Tenant Profiles created during the run are removed.
- The suite assumes a clean model (these override applications and the secret do not
  already exist) and never removes/reconfigures the pre-existing global `s3-integrator`,
  `resource-dispatcher`, or any pre-existing `data-kubeflow-integrator`.
