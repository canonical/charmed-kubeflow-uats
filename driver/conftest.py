# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

from _pytest.config.argparsing import Parser


def pytest_addoption(parser: Parser):
    """Add pytest options.

    * Add a `--filter` option to (de)select test cases based on their name (see also
      https://docs.pytest.org/en/7.4.x/reference/reference.html#command-line-flags)
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
        default=False,
        help="Include GPU tests under `gpu` directory and enable them by using a tensorflow image"
         " and scheduling the pod on a node with a GPU",
    )
