# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

from _pytest.config.argparsing import Parser


def pytest_addoption(parser: Parser):
    """Add pytest options.

    * Add a `--filter` option to (de)select test cases based on their name (see also
      https://docs.pytest.org/en/7.4.x/reference/reference.html#command-line-flags)
    * Add a `--branch` option to select which remote branch to run tests from (see also
      https://github.com/canonical/charmed-kubeflow-uats/issues/65#issuecomment-2016884170).
      Defaults to `main`.
    """
    parser.addoption(
        "--filter",
        help="Provide a filter to (de)select tests cases based on their name. The filter follows"
        " the same syntax as the pytest `-k` option, e.g. --filter 'kfp or katib' will run all"
        " tests containing 'kfp' or 'katib' in their name, whereas --filter 'not kserve' will run"
        " any test that doesn't contain 'kserve' in its name. Essentially, the option simulates"
        " the behaviour of running `pytest -k '<filter>'` directly on the test suite.",
    )
    parser.addoption(
        "--branch",
        help="Provide a remote branch from charmed-kubeflow-uats repository which will be used "
        " to checkout and run the UATs from.",
    )
