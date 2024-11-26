# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import os

import nbformat
import pytest
from nbclient.exceptions import CellExecutionError
from nbconvert.preprocessors import ExecutePreprocessor
from utils import (
    discover_notebooks,
    format_error_message,
    install_python_requirements,
    save_notebook,
)

EXAMPLES_DIR = {"cpu": "notebooks/cpu", "gpu": "notebooks/gpu"}
INCLUDE_GPU_TESTS = os.getenv("include_gpu_tests").lower() == "true"

NOTEBOOKS = discover_notebooks(EXAMPLES_DIR["cpu"])
if INCLUDE_GPU_TESTS:
    NOTEBOOKS.update(discover_notebooks(EXAMPLES_DIR["gpu"]))

log = logging.getLogger(__name__)


@pytest.mark.ipynb
@pytest.mark.parametrize(
    # notebook - ipynb file to execute
    "test_notebook",
    NOTEBOOKS.values(),
    ids=NOTEBOOKS.keys(),
)
def test_notebook(test_notebook):
    """Test Notebook Generic Wrapper."""
    os.chdir(os.path.dirname(test_notebook))

    with open(test_notebook) as nb:
        notebook = nbformat.read(nb, as_version=nbformat.NO_CONVERT)

    ep = ExecutePreprocessor(
        timeout=-1, kernel_name="python3", on_notebook_start=install_python_requirements
    )
    ep.skip_cells_with_tag = "pytest-skip"

    if INCLUDE_GPU_TESTS:
        log.info(
            "Note that only CPU tests will be run. In order to run tests that use an NVIDIA GPU,"
            "use the `--include-gpu-tests` flag e.g. `tox -e kubeflow-local -- --include-gpu-tests`."
            "To learn more, use `--help` or refer to the repository's README file."
        )

    try:
        log.info(f"Running {os.path.basename(test_notebook)}...")
        output_notebook, _ = ep.preprocess(notebook, {"metadata": {"path": "./"}})
        # persist the notebook output to the original file for debugging purposes
        save_notebook(output_notebook, test_notebook)
    except CellExecutionError as e:
        # handle underlying error
        pytest.fail(f"Notebook execution failed with {e.ename}: {e.evalue}")

    for cell in output_notebook.cells:
        metadata = cell.get("metadata", dict)
        if "raises-exception" in metadata.get("tags", []):
            for cell_output in cell.outputs:
                if cell_output.output_type == "error":
                    # extract the error message from the cell output
                    log.error(format_error_message(cell_output.traceback))
                    pytest.fail(cell_output.traceback[-1])
