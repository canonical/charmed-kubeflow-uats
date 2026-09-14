# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Conftest for M2M identity integration tests."""

import logging
import sys
from pathlib import Path

import pytest

# Add the driver directory to path so shared modules (utils, ingress) are importable.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

log = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
def log_test_boundaries(request: pytest.FixtureRequest):
    """Log when each test starts and ends."""
    log.info(f"----- START {request.node.nodeid} -----")
    yield
    log.info(f"----- END {request.node.nodeid} -----")
