# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import subprocess
from typing import Dict

import tenacity
from lightkube import ApiError, Client, codecs
from lightkube.generic_resource import create_global_resource, create_namespaced_resource
from lightkube.resources.batch_v1 import Job
from lightkube.resources.core_v1 import Namespace

PROFILE_RESOURCE = create_global_resource(
    group="kubeflow.org",
    version="v1",
    kind="profile",
    plural="profiles",
)

log = logging.getLogger(__name__)

PODDEFAULT_RESOURCE = create_namespaced_resource(
    group="kubeflow.org",
    version="v1alpha1",
    kind="poddefault",
    plural="poddefaults",
)


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=2, min=1, max=10),
    stop=tenacity.stop_after_attempt(30),
    reraise=True,
)
def assert_namespace_active(
    client: Client,
    namespace: str,
):
    """Test that the provided namespace is Active.

    Retries multiple times to allow for the K8s namespace to be created and reach Active status.
    """
    # raises a 404 ApiError if the namespace doesn't exist
    ns = client.get(Namespace, namespace)
    phase = ns.status.phase

    log.info(f"Waiting for namespace {namespace} to become 'Active': phase == {phase}")
    assert phase == "Active", f"Waited too long for namespace {namespace}!"


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=2, min=1, max=10),
    stop=tenacity.stop_after_attempt(15),
    reraise=True,
)
def assert_poddefault_created_in_namespace(
    client: Client,
    name: str,
    namespace: str,
):
    """Test that the given namespace contains the PodDefault required.

    Retries multiple times to allow for the PodDefault to be synced to the namespace.
    """
    pod_default = None
    try:
        pod_default = client.get(PODDEFAULT_RESOURCE, name, namespace=namespace)
    except ApiError:
        log.info(f"Waiting for PodDefault {name} to be created in namespace {namespace}..")
    assert pod_default is not None, f"Waited too long for PodDefault {name} to be created."


def _log_before_sleep(retry_state):
    """Custom callback to log the number of seconds before the next attempt."""
    next_attempt = retry_state.attempt_number
    delay = retry_state.next_action.sleep
    log.info(f"Retrying in {int(delay)} seconds (attempts: {next_attempt})")


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=2, min=1, max=32),
    retry=tenacity.retry_if_not_result(lambda result: result),
    stop=tenacity.stop_after_delay(60 * 60),
    before_sleep=_log_before_sleep,
    reraise=True,
)
def wait_for_job(
    client: Client,
    job_name: str,
    namespace: str,
):
    """Wait for a Kubernetes Job to complete.

    Keep retrying (up to a maximum of 3600 seconds) while the Job is active or just not yet ready,
    and stop once it becomes successful. This is implemented using the built-in
    `retry_if_not_result` tenacity function, along with `wait_for_job` returning False or True,
    respectively.

    If the Job fails or lands in an unexpected state, this function will raise a ValueError and
    fail immediately.
    """
    # raises a 404 ApiError if the Job doesn't exist
    job = client.get(Job, name=job_name, namespace=namespace)
    if job.status.succeeded:
        # stop retrying, Job succeeded
        log.info(f"Job {namespace}/{job_name} completed successfully!")
        return True
    elif job.status.failed:
        raise ValueError(f"Job {namespace}/{job_name} failed!")
    elif not job.status.ready or job.status.active:
        # continue retrying
        status = "active" if job.status.active else "not ready"
        log.info(f"Waiting for Job {namespace}/{job_name} to complete (status == {status})")
        return False
    else:
        raise ValueError(f"Unknown status {job.status} for Job {namespace}/{job_name}!")


def fetch_job_logs(job_name, namespace, tests_local_run):
    """Fetch the logs produced by a Kubernetes Job."""
    if not tests_local_run:
        print("##### git-sync initContainer logs #####")
        command = ["kubectl", "logs", "-n", namespace, f"job/{job_name}", "-c", "git-sync"]
        subprocess.check_call(command)

    print("##### test-kubeflow container logs #####")
    command = ["kubectl", "logs", "-n", namespace, f"job/{job_name}"]
    subprocess.check_call(command)


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=2, min=1, max=10),
    stop=tenacity.stop_after_attempt(10),
    reraise=True,
)
def assert_profile_deleted(client, profile_name, logger: logging.Logger):
    """Assert that the Profile is deleted.

    Retries multiple times to allow for the Profile to be deleted.
    """
    deleted = False
    try:
        client.get(PROFILE_RESOURCE, profile_name)
    except ApiError as error:
        if error.status.code != 404:
            logger.info(f"Unable to get Profile {profile_name} (status: {error.status.code})")
            raise
        else:
            deleted = True

    logger.info(f"Waiting for Profile {profile_name} to be deleted..")

    assert deleted, f"Waited too long for Profile {profile_name} to be deleted!"


def context_from(argument: str, request) -> Dict[str, str]:
    """Return a dictionary with key-value entries from the CLI argument."""
    context = {}
    for pair in request.config.getoption(argument):
        key, value = pair.split("=")
        context[key] = value
    return context


def create_poddefault(
    poddefault_path: str, poddefault_context: Dict[str, str], namespace: str, lightkube_client
):
    """Apply the PodDefault from the path after rendering it with the passed context.

    Apply the PodDefault from the path after rendering it with the passed context.
    Once execution is complete, delete the created poddefault.
    """
    poddefault_resource = codecs.load_all_yaml(
        poddefault_path.read_text(),
        poddefault_context,
    )
    poddefault_name = poddefault_resource[0].metadata.name
    log.info(f"Adding {poddefault_name} PodDefault...")
    # Using the first item of the list of poddefault_resource. It is a one item list.
    lightkube_client.create(poddefault_resource[0], namespace=namespace)

    yield

    # delete the PodDefault at the end of the module tests
    poddefault_resource = codecs.load_all_yaml(
        poddefault_path.read_text(),
        poddefault_context,
    )
    poddefault_name = poddefault_resource[0].metadata.name
    log.info(f"Deleting {poddefault_name} PodDefault...")
    lightkube_client.delete(PODDEFAULT_RESOURCE, name=poddefault_name, namespace=namespace)
