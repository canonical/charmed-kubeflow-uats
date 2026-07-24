# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import subprocess

import nbformat


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


def render_notebook_html(notebook, file_path):
    """Render an executed notebook to a standalone HTML file (best effort)."""
    try:
        from nbconvert import HTMLExporter

        html, _ = HTMLExporter().from_notebook_node(notebook)
        with open(file_path, "w", encoding="utf-8") as html_file:
            html_file.write(html)
    except Exception as error:
        print(f"Could not render HTML for {file_path}: {error}")


def emit_result_marker(name, status, failing_cell, error):
    """Print a machine-readable result marker to stdout for the driver to parse."""
    payload = {
        "notebook": name,
        "status": status,
        "failing_cell": failing_cell,
        "error": error,
    }
    print(f"===UAT-RESULT==={json.dumps(payload)}===END-UAT-RESULT===", flush=True)


def save_notebook(notebook, file_path):
    """Save notebook to a file."""
    with open(file_path, "w", encoding="utf-8") as nb_file:
        nbformat.write(notebook, nb_file)
