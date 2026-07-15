# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Multi-tenancy integration tests for Charmed Kubeflow.

Phase 1 stands up an override ``s3-integrator`` and a ``data-kubeflow-integrator`` wired
to the existing ``resource-dispatcher``. Phase 2 asserts per-tenant KFP object-store
isolation. See ``multitenancy-test-suite-design.md`` at the repo root for the full
design.
"""

import base64
import logging
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import jubilant
import kfp
import kfp_server_api
import pytest
import tenacity
from dotenv import load_dotenv
from lightkube import ApiError, Client, codecs
from lightkube.generic_resource import load_in_cluster_generic_resources
from lightkube.models.core_v1 import Container, PodSpec
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.core_v1 import ConfigMap, Pod, Secret
from lightkube.types import CascadeType
from s3 import S3BucketWrapper
from utils import (
    PROFILE_RESOURCE,
    assert_namespace_active,
    assert_pod_running,
    assert_profile_deleted,
    assert_service_account_exists,
    exec_in_pod,
)

log = logging.getLogger(__name__)

logging.getLogger("jubilant.wait").setLevel(logging.WARNING)

# Load variables from a .env file at the repo root if present. Real environment
# variables take precedence.
load_dotenv()

# All tests depend on the bundle being healthy first (shared dependency root).
pytestmark = pytest.mark.dependency(
    depends=["driver/test_kubeflow_workloads.py::test_bundle_correctness"],
    scope="session",
)

ASSETS_DIR = Path(__file__).parent.parent.parent / "assets"
PROFILE_TEMPLATE_FILE = ASSETS_DIR / "ambient-profile.yaml.j2"
PIPELINES_DIR = Path(__file__).parent / "assets"
DATA_PASSING_PIPELINE = str(PIPELINES_DIR / "data_passing_pipeline.yaml")

# Application names.
S3_OVERRIDE_APP = "s3-integrator-override"
# The existing (global) s3-integrator deployed with Kubeflow is always named this.
S3_GLOBAL_APP = "s3-integrator-kfp"
# Distinct name so we never clash with a pre-existing data-kubeflow-integrator.
DATA_INTEGRATOR_APP = "data-kubeflow-integrator-override"
RESOURCE_DISPATCHER_APP = "resource-dispatcher"
S3_SECRET_NAME = "s3_secret_kfp_override"

# Fallback channel used only when the deployed channel cannot be detected.
DEFAULT_CHANNEL = "latest/stable"

# Tenant profiles (namespaces).
PROFILE_TENANT_OVERRIDE = "mt-tenant-override"
PROFILE_TENANT_GLOBAL = "mt-tenant-global"

# Resources the integrator dispatches into a tenant namespace (from PR #34).
KFP_ARTIFACT_SECRET = "mlpipeline-minio-artifact"
KFP_LAUNCHER_CONFIGMAP = "kfp-launcher"
KFP_ARTIFACT_REPOS_CONFIGMAP = "artifact-repositories"

# Bucket names from the environment (populated by load_dotenv above).
BUCKET_OVERRIDE = os.environ.get("S3_BUCKET_KFP_OVERRIDE")
BUCKET_GLOBAL = os.environ.get("S3_BUCKET_KFP_GLOBAL")

CURL_POD_NAME = "mt-tenant-curl"

# Charms that indicate the Istio Ambient Mesh is in use.
_AMBIENT_MESH_APPS = ("istio-beacon-k8s",)


def detect_channel(juju: jubilant.Juju, app_name: str, default: str = DEFAULT_CHANNEL) -> str:
    """Return the charm-channel of a deployed app, or ``default`` if not present."""
    app = juju.status().apps.get(app_name)
    if app and app.charm_channel:
        return app.charm_channel
    return default


def kfp_client_for(namespace: str) -> kfp.Client:
    """Return a KFP client impersonating ``{namespace}@email.com``."""
    client = kfp.Client(host="http://localhost:8080", namespace=namespace)
    client.runs.api_client.default_headers.update({"kubeflow-userid": f"{namespace}@email.com"})
    return client


def s3_wrapper_for(endpoint: str, access_key: str, secret_key: str) -> S3BucketWrapper:
    """Build an ``S3BucketWrapper`` from an endpoint URL and credentials."""
    parsed = urlparse(endpoint)
    return S3BucketWrapper(
        access_key=access_key,
        secret_access_key=secret_key,
        s3_service=parsed.hostname,
        s3_port=parsed.port or (443 if parsed.scheme == "https" else 80),
        secure=parsed.scheme == "https",
    )


def list_object_keys(wrapper: S3BucketWrapper, bucket: str) -> set[str]:
    """Return the set of object keys currently in ``bucket``."""
    objs = wrapper.client.list_objects_v2(Bucket=bucket).get("Contents", [])
    return {obj["Key"] for obj in objs}


def _ambient_mesh_enabled(juju: jubilant.Juju) -> bool:
    """Return True if the deployment appears to use the Istio Ambient Mesh."""
    apps = juju.status().apps
    return any(name in apps for name in _AMBIENT_MESH_APPS)


def _create_profile(client: Client, namespace: str) -> None:
    """Create a Kubeflow Profile and wait for its namespace to be ready."""
    profile = list(
        codecs.load_all_yaml(
            PROFILE_TEMPLATE_FILE.read_text(),
            context={"namespace": namespace},
        )
    )[0]
    client.create(profile)
    assert_namespace_active(client, namespace)
    assert_service_account_exists(client, "default-editor", namespace)


def _delete_profile(client: Client, namespace: str) -> None:
    """Delete a Kubeflow Profile, tolerating a missing one."""
    try:
        client.delete(PROFILE_RESOURCE, name=namespace, cascade=CascadeType.FOREGROUND)
        assert_profile_deleted(client, namespace, log)
    except ApiError as error:
        if error.status.code != 404:
            raise


@tenacity.retry(
    wait=tenacity.wait_fixed(5),
    stop=tenacity.stop_after_delay(300),
    retry=tenacity.retry_if_exception_type(ApiError),
    reraise=True,
)
def wait_for_resource(client: Client, resource, name: str, namespace: str):
    """Return a namespaced resource once it exists, retrying on 404."""
    return client.get(resource, name=name, namespace=namespace)


@pytest.fixture(scope="module")
def juju(request: pytest.FixtureRequest) -> jubilant.Juju:
    """Return a jubilant.Juju bound to the already-deployed Kubeflow model."""
    model = request.config.getoption("--model") or "kubeflow"
    instance = jubilant.Juju(model=model)
    instance.wait_timeout = 20 * 60
    return instance


@pytest.fixture(scope="module")
def lightkube_client() -> Client:
    """Initialise a Lightkube client with in-cluster generic resources loaded."""
    client = Client(trust_env=False)
    load_in_cluster_generic_resources(client)
    return client


@pytest.fixture(scope="module")
def s3_override_config() -> dict[str, str]:
    """Read and validate the override S3 credentials from the environment."""
    required = {
        "access-key": "S3_ACCESS_KEY_OVERRIDE",
        "secret-key": "S3_SECRET_KEY_OVERRIDE",
        "endpoint": "S3_SERVER_URL_OVERRIDE",
        "bucket": "S3_BUCKET_KFP_OVERRIDE",
    }
    values = {}
    missing = []
    for key, env_name in required.items():
        value = os.environ.get(env_name)
        if not value:
            missing.append(env_name)
        values[key] = value
    if missing:
        pytest.fail(f"Missing required environment variables: {', '.join(missing)}")
    return values


@pytest.fixture(scope="module")
def deploy_override_s3(juju: jubilant.Juju, s3_override_config: dict[str, str]):
    """Deploy and configure the override s3-integrator (no teardown)."""
    # Create and grant a user secret holding the credentials.
    secret_uri = juju.add_secret(
        S3_SECRET_NAME,
        {
            "access-key": s3_override_config["access-key"],
            "secret-key": s3_override_config["secret-key"],
        },
    )
    juju.grant_secret(S3_SECRET_NAME, S3_OVERRIDE_APP)

    # Deploy under a distinct name, reusing the global s3-integrator channel.
    channel = detect_channel(juju, S3_GLOBAL_APP)
    juju.deploy("s3-integrator", app=S3_OVERRIDE_APP, channel=channel, trust=True)

    # Configure endpoint, bucket, and credentials secret.
    juju.config(
        S3_OVERRIDE_APP,
        {
            "endpoint": s3_override_config["endpoint"],
            "bucket": s3_override_config["bucket"],
            "credentials": secret_uri,
        },
    )
    juju.wait(jubilant.all_active, delay=10)
    yield S3_OVERRIDE_APP


@pytest.fixture(scope="module")
def deploy_data_integrator(juju: jubilant.Juju, deploy_override_s3: str):
    """Deploy data-kubeflow-integrator and wire its relations (no teardown)."""
    channel = detect_channel(juju, "data-kubeflow-integrator")
    juju.deploy(
        "data-kubeflow-integrator",
        app=DATA_INTEGRATOR_APP,
        channel=channel,
        trust=True,
    )

    # Integrate with the override s3-integrator (object-store credentials source).
    juju.integrate(f"{DATA_INTEGRATOR_APP}:kfp-s3-storage", f"{S3_OVERRIDE_APP}:s3-credentials")

    # Integrate with the existing resource-dispatcher over both relations.
    juju.integrate(f"{DATA_INTEGRATOR_APP}:secrets", f"{RESOURCE_DISPATCHER_APP}:secrets")
    juju.integrate(f"{DATA_INTEGRATOR_APP}:config-maps", f"{RESOURCE_DISPATCHER_APP}:config-maps")
    juju.wait(jubilant.all_active, delay=10)
    yield DATA_INTEGRATOR_APP


@pytest.fixture(scope="module")
def setup_tenant_override(
    juju: jubilant.Juju, lightkube_client: Client, deploy_data_integrator: str
):
    """Create the override tenant Profile and bind the integrator to it."""
    _create_profile(lightkube_client, PROFILE_TENANT_OVERRIDE)
    juju.config(DATA_INTEGRATOR_APP, {"profile": PROFILE_TENANT_OVERRIDE})
    juju.wait(jubilant.all_active, delay=10)
    yield PROFILE_TENANT_OVERRIDE
    _delete_profile(lightkube_client, PROFILE_TENANT_OVERRIDE)


@pytest.fixture(scope="module")
def setup_tenant_global(lightkube_client: Client, deploy_data_integrator: str):
    """Create the global tenant Profile (target namespace for denial tests)."""
    _create_profile(lightkube_client, PROFILE_TENANT_GLOBAL)
    yield PROFILE_TENANT_GLOBAL
    _delete_profile(lightkube_client, PROFILE_TENANT_GLOBAL)


@pytest.fixture(scope="module")
def forward_kfp_ui(juju: jubilant.Juju):
    """Port-forward the kfp-ui service to localhost:8080 for the KFP client."""
    proc = subprocess.Popen(
        ["kubectl", "port-forward", "-n", juju.model, "svc/kfp-ui", "8080:3000"]
    )
    time.sleep(6)  # allow the port-forward to establish
    yield
    proc.terminate()


@pytest.fixture(scope="module")
def s3_override(s3_override_config: dict[str, str]) -> S3BucketWrapper:
    """Return an S3 wrapper for the override tenant's bucket."""
    return s3_wrapper_for(
        s3_override_config["endpoint"],
        s3_override_config["access-key"],
        s3_override_config["secret-key"],
    )


@pytest.fixture(scope="module")
def s3_global() -> S3BucketWrapper:
    """Return an S3 wrapper for the global tenant's bucket."""
    endpoint = os.environ.get("S3_SERVER_URL_GLOBAL")
    access_key = os.environ.get("S3_ACCESS_KEY_GLOBAL")
    secret_key = os.environ.get("S3_SECRET_KEY_GLOBAL")
    if not all([endpoint, access_key, secret_key, BUCKET_GLOBAL]):
        pytest.fail("Missing required S3_*_GLOBAL environment variables")
    return s3_wrapper_for(endpoint, access_key, secret_key)


@pytest.fixture(scope="module")
def curl_pod_in_override(lightkube_client: Client, setup_tenant_override: str):
    """Create a curl pod inside the override tenant namespace."""
    pod = Pod(
        metadata=ObjectMeta(name=CURL_POD_NAME, namespace=PROFILE_TENANT_OVERRIDE),
        spec=PodSpec(
            serviceAccountName="default-editor",
            restartPolicy="Never",
            containers=[
                Container(
                    name="curl",
                    image="curlimages/curl:latest",
                    command=["sleep", "infinity"],
                )
            ],
        ),
    )
    lightkube_client.create(pod, namespace=PROFILE_TENANT_OVERRIDE)
    assert_pod_running(lightkube_client, CURL_POD_NAME, PROFILE_TENANT_OVERRIDE)
    yield CURL_POD_NAME
    try:
        lightkube_client.delete(Pod, name=CURL_POD_NAME, namespace=PROFILE_TENANT_OVERRIDE)
    except ApiError as error:
        if error.status.code != 404:
            raise


def test_multi_tenancy_infrastructure_ready(juju: jubilant.Juju, deploy_data_integrator: str):
    """Phase 1: override s3-integrator + integrator deployed and integrated."""
    status = juju.status()
    for app in (S3_OVERRIDE_APP, DATA_INTEGRATOR_APP, RESOURCE_DISPATCHER_APP):
        assert app in status.apps, f"Application {app} not found in model"
        current = status.apps[app].app_status.current
        assert current == "active", f"Expected {app} active, got {current}"


def test_tenant_kfp_resources_dispatched(lightkube_client: Client, setup_tenant_override: str):
    """The integrator dispatches per-tenant KFP resources bound to the override S3."""
    ns = PROFILE_TENANT_OVERRIDE
    secret = wait_for_resource(lightkube_client, Secret, KFP_ARTIFACT_SECRET, ns)
    launcher = wait_for_resource(lightkube_client, ConfigMap, KFP_LAUNCHER_CONFIGMAP, ns)
    wait_for_resource(lightkube_client, ConfigMap, KFP_ARTIFACT_REPOS_CONFIGMAP, ns)

    # The artifact Secret carries the OVERRIDE credentials, not the global ones.
    decoded = {key: base64.b64decode(value).decode() for key, value in (secret.data or {}).items()}
    assert os.environ["S3_ACCESS_KEY_OVERRIDE"] in decoded.values()
    global_access = os.environ.get("S3_ACCESS_KEY_GLOBAL")
    if global_access:
        assert global_access not in decoded.values()

    # kfp-launcher / artifact-repositories point at the OVERRIDE bucket + endpoint.
    launcher_blob = str(launcher.data)
    assert os.environ["S3_BUCKET_KFP_OVERRIDE"] in launcher_blob
    if BUCKET_GLOBAL:
        assert BUCKET_GLOBAL not in launcher_blob


def test_tenant_pipeline_artifacts_isolated_to_own_bucket(
    setup_tenant_override: str,
    forward_kfp_ui,
    s3_override: S3BucketWrapper,
    s3_global: S3BucketWrapper,
):
    """A pipeline run as the override tenant stores artifacts only in its own bucket."""
    kfp_client = kfp_client_for(PROFILE_TENANT_OVERRIDE)
    before_override = list_object_keys(s3_override, BUCKET_OVERRIDE)
    before_global = list_object_keys(s3_global, BUCKET_GLOBAL)

    experiment = kfp_client.create_experiment(
        name="mt-test-experiment", namespace=PROFILE_TENANT_OVERRIDE
    )
    run = kfp_client.create_run_from_pipeline_package(
        pipeline_file=DATA_PASSING_PIPELINE,
        arguments={},
        run_name="mt-artifact-run",
        experiment_name=experiment.display_name,
        namespace=PROFILE_TENANT_OVERRIDE,
    )
    result = kfp_client.wait_for_run_completion(run.run_id, timeout=600)
    assert result.state == "SUCCEEDED"

    after_override = list_object_keys(s3_override, BUCKET_OVERRIDE)
    after_global = list_object_keys(s3_global, BUCKET_GLOBAL)

    assert after_override - before_override, "expected new artifacts in override bucket"
    assert after_global == before_global, "no artifacts should land in the global bucket"


def test_cross_tenant_bucket_isolation():
    """The two tenants use distinct object stores."""
    assert (BUCKET_OVERRIDE, os.environ.get("S3_SERVER_URL_OVERRIDE")) != (
        BUCKET_GLOBAL,
        os.environ.get("S3_SERVER_URL_GLOBAL"),
    )


def test_cross_tenant_kfp_api_denied(
    forward_kfp_ui, setup_tenant_override: str, setup_tenant_global: str
):
    """The override tenant identity cannot list the global tenant's experiments."""
    kfp_client = kfp_client_for(PROFILE_TENANT_OVERRIDE)
    with pytest.raises(kfp_server_api.ApiException) as exc:
        kfp_client.list_experiments(namespace=PROFILE_TENANT_GLOBAL)
    assert exc.value.status in (401, 403), f"expected auth denial, got {exc.value.status}"


def test_tenant_pipeline_run_succeeds(setup_tenant_override: str, forward_kfp_ui):
    """The override tenant can run a pipeline against its own object store."""
    kfp_client = kfp_client_for(PROFILE_TENANT_OVERRIDE)
    experiment = kfp_client.create_experiment(name="mt-smoke", namespace=PROFILE_TENANT_OVERRIDE)
    run = kfp_client.create_run_from_pipeline_package(
        pipeline_file=DATA_PASSING_PIPELINE,
        arguments={},
        run_name="mt-smoke-run",
        experiment_name=experiment.display_name,
        namespace=PROFILE_TENANT_OVERRIDE,
    )
    result = kfp_client.wait_for_run_completion(run.run_id, timeout=600)
    assert result.state == "SUCCEEDED"


def test_cross_tenant_kfp_api_denied_within_mesh(
    juju: jubilant.Juju, setup_tenant_global: str, curl_pod_in_override: str
):
    """Ambient mesh blocks kubeflow-userid spoofing from another tenant's pod."""
    if not _ambient_mesh_enabled(juju):
        pytest.skip("requires Istio Ambient Mesh")

    # Impersonate the GLOBAL tenant user from inside the OVERRIDE tenant pod.
    curl_command = [
        "curl",
        "-s",
        "-w",
        "\\nHTTP_CODE:%{http_code}",
        "-H",
        f"kubeflow-userid: {PROFILE_TENANT_GLOBAL}@email.com",
        f"ml-pipeline.{juju.model}.svc:8888/apis/v2beta1/experiments"
        f"?namespace={PROFILE_TENANT_GLOBAL}",
    ]
    stdout, _stderr, _returncode = exec_in_pod(
        curl_pod_in_override, PROFILE_TENANT_OVERRIDE, curl_command
    )

    http_code = int(stdout.split("HTTP_CODE:")[-1].strip())
    assert http_code == 403, f"expected 403 (RBAC denied), got {http_code}: {stdout}"
    assert "rbac: access denied" in stdout.lower()
