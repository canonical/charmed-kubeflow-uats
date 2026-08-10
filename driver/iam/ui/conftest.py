# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Conftest for UI (Identity login) integration tests."""

import sys
from pathlib import Path

import pytest

# Add this directory first (so `from helpers import` resolves to iam/ui/helpers.py,
# not iam/m2m/helpers.py when collecting the whole driver/ tree), then the driver
# directory to share fixtures/utils with the main driver.
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(1, str(Path(__file__).parent.parent.parent))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Expose the call-phase report on the item so fixtures can act on test outcome.

    Used by the ``context`` fixture to decide whether to retain the Playwright trace
    and screenshot (only on failure).
    """
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)
