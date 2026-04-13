# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest
from _pytest.config.argparsing import Parser

BUNDLE_URL_SIDECAR = "file:assets/versions-sidecar.yaml"
BUNDLE_URL_AMBIENT = "file:assets/versions-ambient.yaml"
TESTS_IMAGE = "ghcr.io/kubeflow/kubeflow/notebook-servers/jupyter-scipy:v1.10.0"


def pytest_addoption(parser: Parser):
    """Add pytest options.

    * Add a `--proxy` option that enables setting `http_proxy`, `https_proxy` and
      `no_proxy` environment variables.
    * Add a `--filter` option to (de)select test cases based on their name (see also
      https://docs.pytest.org/en/7.4.x/reference/reference.html#command-line-flags)
    * Add an `--include-gpu-tests` flag to include the tests under the `gpu` directory
      in the executed tests.
    * Add an `--include-kubeflow-trainer-tests` flag to include the tests for Kubeflow Trainer V2
      in the executed tests.
    * Add a `--toleration` option that enables setting a `toleration` entry for pods
      with the enable-gpu = 'true' label.
    * Add a `--k8s-default-runtimeclass-handler` option to specify the default RuntimeClass handler
      of your Kubernetes cluster. The default one for MicroK8s is otherwise assumed.
    * Add a `--security-policy` option to specify the security policy (privileged or baseline)
      defined in `kubeflow-profiles` for the testing namespace.
    * Add a `--kubeflow-model` option to specify the juju model where kubeflow is deployed.
    * Add a `--test-image` option to specify the test image to be used by the driver notebook pod.
    * Add an `--include-ambient-tests` flag to include the ambient integration tests in the
      executed tests.
    """
    parser.addoption(
        "--proxy",
        nargs=3,
        metavar=("http_proxy", "https_proxy", "no_proxy"),
        help="Set a number of key-value pairs for the proxy environment variables."
        " Example: "
        "--proxy http_proxy='proxy:port' https_proxy='proxy:port' no_proxy=<comma separated of no proxy>'"
        " If used, a PodDefault will be rendered and applied to the Kubernetes deployment."
        " It is not used by default.",
        action="store",
    )
    parser.addoption(
        "--filter",
        help="Provide a filter to (de)select tests cases based on their name. The filter follows"
        " the same syntax as the pytest `-k` option, e.g. --filter 'kfp or katib' will run all"
        " tests containing 'kfp' or 'katib' in their name, whereas --filter 'not kserve' will run"
        " any test that doesn't contain 'kserve' in its name. Essentially, the option simulates"
        " the behaviour of running `pytest -k '<filter>'` directly on the test suite.",
    )
    parser.addoption(
        "--include-gpu-tests",
        action="store_true",
        help="Defines whether to include the tests under the `gpu` directory in the executed tests."
        "By default, it is set to False.",
    )
    parser.addoption(
        "--include-kubeflow-trainer-tests",
        action="store_true",
        help="Defines whether to include the tests for Kubeflow Trainer V2 in the executed tests."
        "By default, it is set to False.",
    )
    parser.addoption(
        "--toleration",
        nargs="+",
        help="Set a number of key-value pairs for the toleration needed to access a GPU node. With the"
        " use of a PodDefault, the toleration is set to pods that have the label enable-gpu='true'."
        " Example:"
        " --toleration key='key1' operator='Equal' value='value1' effect='NoSchedule' seconds='3600'."
        " Since most fields are optional, ensure that that the toleration passed is a valid one by"
        " consulting relevant Kubernetes docs:\n"
        " https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/#scheduling.",
        action="store",
    )
    parser.addoption(
        "--k8s-default-runtimeclass-handler",
        default="runc",
        help="Provide the default RuntimeClass handler of your Kubernetes cluster for local tests to"
        " be set up correctly. The default one for MicroK8s is otherwise assumed.",
    )
    parser.addoption(
        "--security-policy",
        choices=["privileged", "baseline"],
        default="privileged",
        metavar=("security_policy"),
        help="Provide the security policy defined in `kubeflow-profiles` to ensure the expected bevahior in the testing namespace."
        " Possible values correspond to Pod Security Standard levels: 'privileged', 'baseline'."
        " For more information, see: \n"
        " https://kubernetes.io/docs/concepts/security/pod-security-standards/",
        action="store",
    )
    parser.addoption(
        "--model",
        default=None,
        help="Provide the exact name of the juju model where kubeflow is deployed."
        " Overridden by --kubeflow-model if both are set.",
    )
    parser.addoption(
        "--kubeflow-model",
        default=None,
        help="Provide the exact name of the juju model where kubeflow is deployed."
        " Overrides --model if both are set.",
    )
    parser.addoption(
        "--bundle",
        default=None,
        help="Provide the bundle to be used during the check. You can use a URL, e.g. http://..., or a local file, file:/path/to/file. If empty, the check is skipped",
    )
    parser.addoption(
        "--test-image",
        default=TESTS_IMAGE,
        help="Provide the test image to be used by the driver notebook pod.",
    )
    parser.addoption(
        "--include-ambient-tests",
        action="store_true",
        help="Defines whether to include the ambient integration tests."
        "By default, it is set to False.",
    )


def pytest_configure(config):
    """Set the default bundle based on whether ambient tests are enabled."""
    if config.getoption("--bundle") is not None:
        return

    if config.getoption("--include-ambient-tests"):
        config.option.bundle = BUNDLE_URL_AMBIENT
    else:
        config.option.bundle = BUNDLE_URL_SIDECAR


def pytest_collection_modifyitems(config, items):
    """Ensure dependency roots are collected before tests that depend on them.

    pytest-dependency skips immediately when a dependency has not run yet,
    so this forces the bundle correctness check to execute first.
    """

    if not config.getoption("--include-ambient-tests", default=False):
        skip_ambient = pytest.mark.skip(reason="need --include-ambient-tests option to run")
        for item in items:
            if "/ambient/" in item.nodeid:
                item.add_marker(skip_ambient)

    dependency_root = "driver/test_kubeflow_workloads.py::test_bundle_correctness"
    items.sort(key=lambda item: 0 if item.nodeid.endswith(dependency_root) else 1)
