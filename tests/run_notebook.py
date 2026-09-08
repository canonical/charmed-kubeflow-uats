# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Execute a single UAT notebook and report success/failure via the exit code.

Run inside each per-notebook Job (``python3 run_notebook.py <path>``). The driver is the
test framework (one Job per notebook, retries, summary); the pod runs one notebook and
signals the outcome through its **exit code** (0 = passed, non-zero = failed). Its logs
are the debugging record. Cells tagged ``pytest-skip`` are skipped; a cell tagged
``raises-exception`` that errors still counts as a failure.
"""

import os
import sys

import nbformat
from nbclient.exceptions import CellExecutionError
from nbconvert.preprocessors import ExecutePreprocessor
from utils import install_python_requirements


def _report_cell_errors(notebook) -> bool:
    """Print the traceback of any errored cell and return whether any cell errored."""
    errored = False
    for cell in notebook.cells:
        for output in cell.get("outputs", []):
            if output.get("output_type") == "error":
                print("".join(output.get("traceback", [])))
                errored = True
    return errored


def notebook_passed(notebook_path) -> bool:
    """Execute one notebook and return whether it passed (no cell errored).

    The notebook's ``requirements.txt`` (if present) is installed before the kernel
    starts, and cells tagged ``pytest-skip`` are skipped.
    """
    os.chdir(os.path.dirname(notebook_path))
    if os.path.exists("requirements.txt"):
        install_python_requirements()

    with open(notebook_path) as handle:
        notebook = nbformat.read(handle, as_version=nbformat.NO_CONVERT)

    ep = ExecutePreprocessor(timeout=-1, kernel_name="python3")
    ep.skip_cells_with_tag = "pytest-skip"
    try:
        ep.preprocess(notebook, {"metadata": {"path": "./"}})
    except CellExecutionError as error:
        # Surface the traceback in the logs, which the driver saves on failure.
        print(error)
        return False

    # A `raises-exception` cell errors without stopping execution above.
    return not _report_cell_errors(notebook)


def main(argv=None) -> int:
    """Execute the notebook and return an exit code (0 passed, 1 failed, 2 usage)."""
    argv = argv if argv is not None else sys.argv[1:]
    notebook_path = argv[0] if argv else os.getenv("NOTEBOOK_PATH")
    if not notebook_path:
        print("usage: run_notebook.py <notebook-path>  (or set NOTEBOOK_PATH)")
        return 2

    name = os.path.splitext(os.path.basename(notebook_path))[0]
    print(f"Running {name}...")
    if notebook_passed(notebook_path):
        print(f"Notebook '{name}' PASSED")
        return 0
    print(f"Notebook '{name}' FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
