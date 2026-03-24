# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Conftest for ambient tests."""

import sys
from pathlib import Path

# Add parent directory to path to share fixtures with main driver
sys.path.insert(0, str(Path(__file__).parent.parent))
