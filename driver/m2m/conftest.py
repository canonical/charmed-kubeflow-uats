# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Conftest for M2M identity integration tests."""

import sys
from pathlib import Path

# Add parent directory to path to share fixtures/utils with the main driver
sys.path.insert(0, str(Path(__file__).parent.parent))
