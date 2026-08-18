# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Conftest for M2M identity integration tests."""

import sys
from pathlib import Path

# Add the driver directory to path so shared modules (utils, ingress) are importable.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
