# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

from _pytest.config.argparsing import Parser


def pytest_addoption(parser: Parser):
    """Add pytest options.

    * Add a `--include-gpu-tests` option to include gpu notebooks in the executed tests.
    """
    parser.addoption(
        "--include-gpu-tests",
        action="store_true",
        default=False,
        help="Include GPU tests under `gpu` directory and enable them by using a tensorflow image"
         " and scheduling the pod on a node with a GPU",
    )
