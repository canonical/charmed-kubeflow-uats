# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import subprocess


def install_python_requirements(requirements_file: str = "requirements.txt"):
    """Install Python dependencies specified in the provided requirements file."""
    subprocess.run(["python3", "-m", "pip", "install", "-r", requirements_file])


def format_error_message(traceback: list):
    """Format error message."""
    return "".join(traceback[-2:])


def first_error_cell_index(notebook):
    """Return the 1-based index of the first cell with an error output, or None."""
    for index, cell in enumerate(notebook.cells, start=1):
        for output in cell.get("outputs", []):
            if output.get("output_type") == "error":
                return index
    return None


def emit_result_marker(name, status, failing_cell, error):
    """Print a machine-readable result marker to stdout for the driver to parse."""
    payload = {
        "notebook": name,
        "status": status,
        "failing_cell": failing_cell,
        "error": error,
    }
    # Format must match _RESULT_RE in driver/notebook_jobs.py.
    print(f"===UAT-RESULT==={json.dumps(payload)}===END-UAT-RESULT===", flush=True)
