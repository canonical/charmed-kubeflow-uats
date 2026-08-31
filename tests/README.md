# Test Kubeflow Integrations

Python notebooks that verify the integration of Kubeflow with different components. The notebooks
are stored in the `notebooks/` directory.

These are regular Python notebooks, which you can view and run manually without any modifications.
They perform simple tasks using the respective APIs and programmatically verify the results: when
everything is as expected their execution is transparent, and in the event of an error they raise
an exception for the execution engine to pick up and report.

## How they are run

The notebooks are executed by the **driver** (see the [top-level README](../README.md)), which runs
each notebook as an isolated Kubernetes Job inside the cluster. Within each Job,
[`run_notebook.py`](run_notebook.py) executes a single notebook, reports the result via its exit
code and a result marker in the logs, and — when requested — collects the executed notebook as an
artifact.

To run the notebooks (or a subset) against a deployed Kubeflow, use the driver via `tox`:

```bash
# run all notebooks
tox -e uats-local

# select a subset by filtering on notebook names (pytest -k syntax)
tox -e uats-local -- --filter "kfp or katib"
tox -e uats-local -- --filter "not kserve"

# include the GPU notebooks (require a GPU node)
tox -e uats-local -- --include-gpu-tests
```

See the [top-level README](../README.md) for the full set of options (bundle selection, proxies,
per-notebook timeout, keeping artifacts, etc.).

## Contributing

When adding new notebooks, see [CONTRIBUTING.md](CONTRIBUTING.md) for the conventions the execution
engine relies on — notably the `raises-exception` tag for verification cells and the `pytest-skip`
tag for cells that should only run outside the automated suite.
