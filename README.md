# Charmed Kubeflow Automated UATs

[![PRs](https://github.com/canonical/charmed-kubeflow-uats/actions/workflows/on_pull_or_push.yaml/badge.svg)](https://github.com/canonical/charmed-kubeflow-uats/actions/workflows/on_pull_or_push.yaml)

Automated User Acceptance Tests (UATs) are essential for evaluating the stability of Charmed
Kubeflow, as well as catching issues early, and are intended to be an invaluable testing tool both
pre-release and post-installation. They combine different components of Charmed Kubeflow in a way
that gives us confidence that everything works as expected, and are meant to be used by end-users
as well as developers alike.

Charmed Kubeflow UATs are broken down in test scenarios implemented as Python notebooks, which are
easy to share, understand, and maintain. We provide a **standalone** test suite included in `tests`
that users can run directly from inside a Notebook with `pytest`, as well as a `driver` that
automates the execution on an existing Kubeflow cluster. More details on running the tests can be
found in the [Run the tests](#run-the-tests) section.

## Prerequisites

Executing the UATs requires a deployed Kubeflow cluster. That said, the deployment and
configuration steps are outside the scope of this project. In other words, the automated tests are
going to assume programmatic access to a Kubeflow installation. Such a deployment consists (at the
very least) of the following pieces:

* A **Kubernetes cluster**, e.g.
    * MicroK8s
    * Charmed Kubernetes
    * EKS cluster
    * AKS cluster <!-- codespell-ignore -->
* **Charmed Kubeflow** deployed on top of it
* **MLFlow (optional)** deployed alongside Kubeflow

For instructions on deploying and getting started with Charmed Kubeflow, we recommend that you
start with [this guide](https://charmed-kubeflow.io/docs/get-started-with-charmed-kubeflow).

The UATs include tests that assume MLFlow is installed alongside Kubeflow, which will otherwise
fail. For instructions on deploying MLFlow you can start with [this
guide](https://discourse.charmhub.io/t/deploying-charmed-mlflow-v2-and-kubeflow-to-eks/10973),
ignoring the EKS specific steps.

## Run the tests

As mentioned before, when it comes to running the tests, you've got 2 options:
* Running the `tests` suite directly with `pytest` inside a Jupyter Notebook
* Running the tests on an existing cluster using the `driver` along with the provided automation

NOTE: Depending on the version of Charmed Kubeflow you want to test, make sure to checkout to the appropriate branch with `git checkout`:
- Charmed Kubeflow 1.8 -> `track/1.8`
- Charmed Kubeflow 1.7 -> `track/1.7`

### Running inside a Notebook

* Create a new Notebook using the `jupyter-scipy` image:
   * Navigate to `Advanced options` > `Configurations`
   * Select all available configurations in order for Kubeflow integrations to work as expected
   * Launch the Notebook and wait for it to be created
* Start a new terminal session and clone this repo locally:

   ```bash
   git clone https://github.com/canonical/charmed-kubeflow-uats.git
   ```
* Navigate to the `tests` directory:

   ```bash
   cd charmed-kubeflow-uats/tests
   ```
* Follow the instructions of the provided [README.md](tests/README.md) to execute the test suite
  with `pytest`

### Running from a configured management environment using the `driver`

To run the tests, Python 3.8 and Tox must be installed on your system. If your default Python version is higher than 3.8, you can set up Python 3.8 with the following commands:

```bash
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt update -y
sudo apt install python3.8 python3.8-distutils python3.8-venv -y
```

Next, create a virtual environment with Python 3.8 and install Tox:

```bash
python3.8 -m venv venv
source venv/bin/activate
pip install tox
```

Next, clone this repo locally and navigate to the repo directory:

```bash
git clone https://github.com/canonical/charmed-kubeflow-uats.git
cd charmed-kubeflow-uats/
```

Then in order to run UATs, there are couple options:

#### Run tests from a remote commit
In this case, tests are fetched from a remote commit of `charmed-kubeflow-uats` repository. In order to define the commit, tests use the hash of the `HEAD`, where the repository is checked out locally. This means that when you want to run tests from a specific branch, you need to check out to that branch and then run the tests. Note that if the locally checked out commit is not pushed to the remote repository, then tests will fail.

```bash
# assumes an existing `kubeflow` Juju model
tox -e uats-remote
```

#### Run tests from local copy

This one works only when running the tests from the same node where the tests job is deployed (e.g. running from the same machine where the Microk8s cluster lives). In this case, the tests job instantiates a volume that is [mounted to the local directory of the repository where tests reside](https://github.com/canonical/charmed-kubeflow-uats/blob/ee0fa08931b11f40e97dbe3e340c413cf466a084/assets/test-job.yaml.j2#L34-L36). If unsure about your setup, use the `-remote` option.

```bash
# assumes an existing `kubeflow` Juju model
tox -e uats-local
```

#### Run a subset of UATs

You can also run a subset of the provided tests using the `--filter` option and passing a filter
that follows the same syntax as the pytest `-k` option, e.g.

```bash
# run any test that doesn't contain 'kserve' in its name
tox -e uats-remote -- --filter "not kserve"
# run all tests containing 'kfp' or 'katib' in their name
tox -e uats-local -- --filter "kfp or katib"
```

This simulates the behaviour of running `pytest -k "some filter"` directly on the test suite.
You can read more about the options provided by Pytest in the corresponding section of the
[documentation](https://docs.pytest.org/en/7.4.x/reference/reference.html#command-line-flags).

#### Run Kubeflow UATs

In order to only run the Kubeflow-specific tests (i.e. no MLFlow integration) you can use the
dedicated `kubeflow` tox test environment:

```bash
# assumes an existing `kubeflow` Juju model
# run tests from the checked out commit after fetching them remotely
tox -e kubeflow-remote
# run tests from the local copy of the repo
tox -e kubeflow-local
```

### Run behind proxy

#### Running using Notebook
To run the tests behind proxy using Notebook:
1. Edit the PodDefault `tests/proxy-poddefault.yaml` to replace the placeholders for:
   * `<proxy_address>:<proxy_port>`: The address and port of your proxy server
   * `<cluster cidr>`: you can get this value by running:
      ```
      cat /var/snap/microk8s/current/args/kube-proxy | grep cluster-cidr
      ```
   * `<service cluster ip range>`: you can get this value by running:
      ```
      cat /var/snap/microk8s/current/args/kube-apiserver | grep service-cluster-ip-range
      ```
   
   * `<nodes internal ip(s)>`: the Internal IP of the nodes where your cluster is running, you can
   get this value by running:
      ```
      microk8s kubectl get nodes -o wide
      ```
      It is the `INTERNAL-IP` value
   * `<hostname>`: the name of your host on which the cluster is deployed, you can use the
   `hostname` command to get it
2. Create a Notebook and from the `Advanced Options > Configurations` select `Add proxy settings`
3. From inside the Notebook, start a new terminal session and clone this repo:

   ```bash
   git clone https://github.com/canonical/charmed-kubeflow-uats.git
   ```
   Open the `charmed-kubeflow-uats/tests` directory and for each `.ipynb` test file there, open it
   and run the Notebook.
   
   Currently, the following tests are supported to run behind proxy:
   * katib
   * kserve
   * kfp_v2
   * training (except TFJob due to https://github.com/canonical/training-operator/issues/182)

#### Developer Notes

Any environment that can be used to access and configure the Charmed Kubeflow deployment is
considered a configured management environment. That is, essentially, any machine with `kubectl`
access to the underlying Kubernetes cluster. This is crucial, since the driver directly depends on
a Kubernetes Job to run the tests. More specifically, the `driver` executes the following steps:
1. Create a Kubeflow Profile (i.e. `test-kubeflow`) to run the tests in
2. Submit a Kubernetes Job (i.e. `test-kubeflow`) that runs `tests`
   The Job performs the following:
   * If a `-local` tox environment is run, then it mounts the local `tests` directory to a Pod that uses `jupyter-scipy` as the container image. Else (in `-remote` tox environments), it creates an emptyDir volume which it syncs to the current commit that the repo is checked out locally, using a [git-sync](https://github.com/kubernetes/git-sync/) `initContainer`.
   * Install python dependencies specified in the [requirements.txt](tests/requirements.txt)
   * Run the test suite by executing `pytest`
3. Wait until the Job completes (regardless of the outcome)
4. Collect and report its logs, corresponding to the `pytest` execution of `tests`
5. Cleanup (remove created Job and Profile)

##### Limitations

With the current implementation we have to wait until the Job completes to fetch its logs. Of
course this makes for a suboptimal UX, since the user might have to wait long before they learn
about the outcome of their tests. Ideally, the Job logs should be streamed directly to the `pytest`
output, providing real-time insight. This is a known limitation that will be addressed in a future
iteration.
