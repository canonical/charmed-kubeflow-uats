# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

import os

from _pytest.config.argparsing import Parser


def pytest_addoption(parser: Parser):
    """Add pytest options.
    * Add an `--include-gpu-tests` flag to include the tests under the `gpu` directory
      in the executed tests.
    """
    parser.addoption(
        "--include-gpu-tests",
        action="store_true",
        help="Defines whether to include tests under the `gpu` directory in the executed tests."
        "By default, it is set to False.",
    )


def pytest_configure(config):
    os.environ["include_gpu_tests"] = str(config.getoption("--include-gpu-tests"))
