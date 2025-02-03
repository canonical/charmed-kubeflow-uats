# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

from _pytest.config.argparsing import Parser


def pytest_addoption(parser: Parser):
    """Add pytest options.

    * Add a `--proxy` option that enables setting `http_proxy`, `https_proxy` and
      `no_proxy` environment variables.
    * Add a `--filter` option to (de)select test cases based on their name (see also
      https://docs.pytest.org/en/7.4.x/reference/reference.html#command-line-flags)
    * Add an `--include-gpu-tests` flag to include the tests under the `gpu` directory
      in the executed tests.
    * Add a `--toleration` option that enables setting a `toleration` entry for pods
      with the enable-gpu = 'true' label.
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
        "--kubeflow-model",
        default="kubeflow",
        help="Provide the name of the namespace/juju model where kubeflow is deployed.",
    )
