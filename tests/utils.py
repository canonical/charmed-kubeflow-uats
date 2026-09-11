# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import subprocess


def install_python_requirements(requirements_file: str = "requirements.txt"):
    """Install Python dependencies specified in the provided requirements file."""
    subprocess.run(["python3", "-m", "pip", "install", "-r", requirements_file])
