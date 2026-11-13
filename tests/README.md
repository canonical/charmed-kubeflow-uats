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
tox -e uats-local

# select a subset by filtering on notebook names (pytest -k syntax)
tox -e uats-local -- --filter "kfp or katib"
tox -e uats-local -- --filter "not kserve"
```

By default only `notebooks/cpu/` is run. Pass these opt-in flags after `--` to include more, and
`--filter` to narrow the resulting set:

- `--include-gpu-tests`: adds `notebooks/gpu/`. Requires a GPU node in the cluster.
- `--include-kubeflow-trainer-tests`: adds `notebooks/kubeflow-trainer/`.

Notebooks under `notebooks/gpu/kubeflow-trainer/` require both flags.

See the [top-level README](../README.md) for the full set of options (bundle selection, proxies,
per-notebook timeout, keeping artifacts, etc.).

## Contributing

When adding new notebooks, see [CONTRIBUTING.md](CONTRIBUTING.md) for the conventions the execution
engine relies on — notably the `raises-exception` tag for verification cells and the `pytest-skip`
tag for cells that should only run outside the automated suite.
