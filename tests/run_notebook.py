# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Execute a single UAT notebook, persist its artifacts, and report the result.

This is the entrypoint run inside each per-notebook Job (``python3 run_notebook.py
<path>``). The driver is the test framework (one Job per notebook, pass/fail, retries,
summary), so the pod only needs to execute one notebook and report back via its exit
code and the ``===UAT-RESULT===`` marker in the logs.
"""

import os
import sys

import nbformat
from nbclient.exceptions import CellExecutionError
from nbconvert.preprocessors import ExecutePreprocessor
from utils import (
    emit_result_marker,
    first_error_cell_index,
    format_error_message,
    install_python_requirements,
    render_notebook_html,
    save_notebook,
)


def _persist_artifacts(notebook, notebook_path, notebook_name, artifacts_dir):
    """Save the executed notebook in place and, if configured, to the artifacts dir."""
    try:
        # persist the notebook output to the original file for debugging purposes
        save_notebook(notebook, notebook_path)
    except PermissionError as error:
        # If the notebook cannot be saved in-place, log the error and continue
        print(f"Permission error while saving notebook: {error}")
    if artifacts_dir:
        os.makedirs(artifacts_dir, exist_ok=True)
        save_notebook(notebook, os.path.join(artifacts_dir, f"{notebook_name}.ipynb"))
        render_notebook_html(notebook, os.path.join(artifacts_dir, f"{notebook_name}.html"))


def _raises_exception_failure(notebook):
    """Return (cell_index, error) for a raises-exception cell that errored, else None."""
    for index, cell in enumerate(notebook.cells, start=1):
        tags = cell.get("metadata", {}).get("tags", [])
        if "raises-exception" in tags:
            for output in cell.outputs:
                if output.output_type == "error":
                    print(format_error_message(output.traceback))
                    return index, format_error_message(output.traceback)
    return None


def execute_notebook(notebook_path, artifacts_dir=None):
    """Execute one notebook and return ``(name, status, failing_cell, error_text)``.

    ``status`` is ``"PASSED"`` or ``"FAILED"``. Cells tagged ``pytest-skip`` are skipped
    and each notebook installs its own ``requirements.txt`` before running.
    """
    notebook_name = os.path.splitext(os.path.basename(notebook_path))[0]
    os.chdir(os.path.dirname(notebook_path))

    with open(notebook_path) as handle:
        notebook = nbformat.read(handle, as_version=nbformat.NO_CONVERT)

    ep = ExecutePreprocessor(
        timeout=-1, kernel_name="python3", on_notebook_start=install_python_requirements
    )
    ep.skip_cells_with_tag = "pytest-skip"

    # nbclient mutates the notebook in place, so keep a reference for saving/inspection
    # even if execution raises partway through.
    executed = notebook
    failing_cell = None
    error_text = ""
    try:
        print(f"Running {notebook_name}...")
        executed, _ = ep.preprocess(notebook, {"metadata": {"path": "./"}})
    except CellExecutionError as error:
        failing_cell = first_error_cell_index(executed)
        error_text = f"{error.ename}: {error.evalue}"
        print(format_error_message(error.traceback))
    finally:
        _persist_artifacts(executed, notebook_path, notebook_name, artifacts_dir)

    # A cell tagged `raises-exception` that still errors is treated as a failure.
    if failing_cell is None:
        raised = _raises_exception_failure(executed)
        if raised:
            failing_cell, error_text = raised

    status = "PASSED" if failing_cell is None and not error_text else "FAILED"
    return notebook_name, status, failing_cell, error_text


def main(argv=None):
    """Execute the notebook, emit the result marker, and return an exit code."""
    argv = argv if argv is not None else sys.argv[1:]
    notebook_path = argv[0] if argv else os.getenv("NOTEBOOK_PATH")
    if not notebook_path:
        print("usage: run_notebook.py <notebook-path>  (or set NOTEBOOK_PATH)")
        return 2

    name, status, failing_cell, error_text = execute_notebook(
        notebook_path, os.getenv("ARTIFACTS_DIR")
    )
    emit_result_marker(name, status, failing_cell, error_text)
    if status != "PASSED":
        print(f"Notebook '{name}' FAILED at cell {failing_cell}: {error_text}")
        return 1
    print(f"Notebook '{name}' PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
