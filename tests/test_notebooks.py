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


log = logging.getLogger(__name__)

EXAMPLES_DIR_CPU = "notebooks/cpu"
EXAMPLES_DIR_GPU = "notebooks/gpu"
NOTEBOOKS_CPU = discover_notebooks(EXAMPLES_DIR_CPU)
NOTEBOOKS_GPU = discover_notebooks(EXAMPLES_DIR_GPU)
INCLUDE_GPU_TESTS = os.getenv("include_gpu_tests").lower()
NOTEBOOKS = { **NOTEBOOKS_CPU, **NOTEBOOKS_GPU} if INCLUDE_GPU_TESTS == "true" else NOTEBOOKS_CPU
###############
print("NOTEBOOKS:")
print(NOTEBOOKS)

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
