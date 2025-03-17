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

## Content
* [Prerequisites](#prerequisites)
* [Run the tests](#run-the-tests)
   * [From inside a notebook](#running-inside-a-notebook)
   * [Using the `driver` ](#running-from-a-configured-management-environment-using-the-driver)
      * [Using a remote commit](#run-tests-from-a-remote-commit)
      * [Using a local copy](#run-tests-from-local-copy)
      * [A subset of UATs](#run-a-subset-of-uats)
      * [Specify a different bundle](#specify-a-different-bundle)
      * [Kubeflow UATs](#run-kubeflow-uats)
      * [MLflow UATs](#run-mlflow-uats)
   * [NVIDIA GPU UAT](#nvidia-gpu-uat)
      * [From inside a notebook](#run-nvidia-gpu-uat-from-inside-a-notebook)
      * [Using the `driver`](#run-nvidia-gpu-uat-using-the-driver)
   * [Behind proxy](#run-behind-proxy)
      * [Prerequisites for KServe UATs](#prerequisites-for-kserve-uats)
      * [From inside a notebook](#running-using-notebook)
      * [Using the `driver`](#running-using-driver)
   * [Developer notes](#developer-notes)
      * [Limitations](#limitations)


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
* **MLflow (optional)** deployed alongside Kubeflow

For instructions on deploying and getting started with Charmed Kubeflow, we recommend that you
start with [this guide](https://charmed-kubeflow.io/docs/get-started-with-charmed-kubeflow).

The UATs include tests that assume MLflow is installed alongside Kubeflow, which will otherwise
fail. For instructions on deploying MLflow you can start with [this
guide](https://documentation.ubuntu.com/charmed-mlflow/en/latest/tutorial/mlflow-kubeflow/)].

When running tests using the driver (see [Using the `driver` ](#running-from-a-configured-management-environment-using-the-driver)), the environment executing the UATs must meet the following requirements:

- Python >=3.10
- Tox
- Juju >=3.6 (required by `pytest-operator`)
- charmcraft >=3.4.3 (required by `pytest-operator`)

Please refer to the respective documentation for more details on how to install these tools on various environments, i.e. the [how to manage Juju](https://canonical-juju.readthedocs-hosted.com/en/latest/user/howto/manage-juju/) and the [setup charmcraft](https://canonical-charmcraft.readthedocs-hosted.com/en/stable/howto/set-up-charmcraft/) user guides.

## Run the tests

As mentioned before, when it comes to running the tests, you've got 2 options:
* Running the `tests` suite directly with `pytest` inside a Jupyter Notebook
* Running the tests on an existing cluster using the `driver` along with the provided automation

NOTE: Depending on the version of Charmed Kubeflow you want to test, make sure to checkout to the appropriate branch with `git checkout`:
- Charmed Kubeflow 1.9 -> `track/1.9`
- Charmed Kubeflow 1.8 -> `track/1.8`
- Charmed Kubeflow 1.7 -> `track/1.7`

The `main` branch is generally used for testing against the `latest/edge` track of the bundle.   

As part of the tests, the UATs checks that the version of the applications are the ones expected for the various tracks. The different branches
above point to a different bundle from the [bundle-kubeflow](https://github.com/canonical/bundle-kubeflow) repository to compare the 
channels in the deployment being tested. The `main` branch allows you to specify the URL of the bundle used for checking by passing the --bundle argument to the tox entrypoints.

### Running inside a Notebook

* Create a new Notebook using the `jupyter-scipy` image:
   * Navigate to `Advanced options` > `Configurations`
   * Select all available configurations in order for Kubeflow integrations to work as expected
   * Launch the Notebook and wait for it to be created
* From inside the Notebook, start a new terminal session and clone this repo locally:

   ```bash
   git clone https://github.com/canonical/charmed-kubeflow-uats.git
   ```
* Navigate to the `tests` directory:

   ```bash
   cd charmed-kubeflow-uats/tests
   ```
* There are two options here:
   1. Follow the instructions of the provided [README.md](tests/README.md) to execute the test suite with `pytest`
   2. For each `.ipynb` test file of interest, open it and run the Notebook.

### Running from a configured management environment using the `driver`

To run the tests, Python >=3.10 and Tox must be installed on your system. You can set up Python 3.10 with the following commands:

```bash
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt update -y
sudo apt install python3.10 python3.10-distutils -y
```

First, create a virtual environment with Python 3.10 and install Tox:

```bash
python3.10 -m venv venv
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

#### Specify a different bundle

To provide a different bundle to be used to check that the deployment has the correct channel version, 
use the `--bundle` flag, e.g.

```bash
tox -e uats-remote -- --bundle <my-bundle>
```

The `<my-bundle>` can be replaced by either a URL, e.g. `http://...`, or a local file, `file:/path/to/file`. Note that the local file path must be accessible when running the tests. 

This flag is currently only provided on main branch and tracks 1.9+. 

#### Run Kubeflow UATs

In order to only run the Kubeflow-specific tests (i.e. no MLflow integration) you can use the
dedicated `kubeflow` tox test environment:

```bash
# assumes an existing `kubeflow` Juju model
# run tests from the checked out commit after fetching them remotely
tox -e kubeflow-remote
# run tests from the local copy of the repo
tox -e kubeflow-local
```

#### Run Mlflow UATs

In order to only run the tests that test integration with MLflow, you can use the
dedicated `mlflow` tox test environment:

```bash
# assumes an existing `kubeflow` Juju model
# run tests from the checked out commit after fetching them remotely
tox -e mlflow-remote
# run tests from the local copy of the repo
tox -e mlflow-local
```

### NVIDIA GPU UAT

#### Run NVIDIA GPU UAT from inside a notebook

##### Prerequisites
If a [taint](https://kubernetes.io/docs/concepts/scheduling-eviction/taint-and-toleration/) is used to prevent scheduling unintended workload to GPU nodes, a toleration is needed in order to enable GPU tests to schedule workloads. To ensure that pods created by GPU tests have the proper toleration:
1. Edit the [PodDefault](./assets/gpu-toleration-poddefault.yaml.j2) to replace the placeholder under `tolerations` with your own toleration e.g.
```
  tolerations:
    - key: "MyKey"
      value: "gpu"
      effect: "NoSchedule"
```
2. Apply the PodDefault to the namespace where you 'll be running the tests in.
   ```
   kubectl apply -f ./assets/gpu-toleration-poddefault.yaml.j2 -n <your_namespace>
   ```

If no taint is used, there are no prerequisites.

##### Steps
In order to run the NVIDIA GPU UAT from inside a notebook, follow the same steps described in the [From inside a notebook](#running-inside-a-notebook) section above.

#### Run NVIDIA GPU UAT using the driver

By default, [GPU UATs](./tests/notebooks/gpu/) are not included in any of the `tox` environments since they require a cluster with a GPU. In order to include those, use the `--include-gpu-tests` flag, e.g.

```bash
# run all tests defined by tox environment `kubeflow` plus those under the 'gpu' directory
tox -e kubeflow-remote -- --include-gpu-tests
# run all tests containing 'kfp' in their name (both cpu and gpu ones)
tox -e uats-remote -- --include-gpu-tests --filter "kfp"
```

As shown in the example above, tests under the `gpu` directory follow the same filters with the rest of the tests.

##### Taints and tolerations

If a [taint](https://kubernetes.io/docs/concepts/scheduling-eviction/taint-and-toleration/) is used to prevent scheduling unintended workload to GPU nodes, a toleration is needed in order to enable GPU tests to schedule workloads. This is achieved via the `--toleration` argument which enables passing the sub-arguments `key, operator, value, effect, seconds`. For example:

```bash
#  Here's an example taint the GPU node may have
#  taints:
#     effect: NoSchedule
#     key: MyKey
#     value: MyValue

tox -e uats-remote -- --include-gpu-tests --toleration key="MyKey" value="MyValue" effect="NoSchedule"
```

The driver will populate the [PodDefault](./assets/gpu-toleration-poddefault.yaml.j2) with the passed toleration values and apply it, ensuring that the toleration is added to workload pods requiring a GPU. Since most fields are optional, make sure that the toleration passed is a valid one by consulting relevant [Kubernetes docs](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/#scheduling).


### Run behind proxy

#### Prerequisites for KServe UATs

To be able to run UATs requiring KServe (e2e-wine, kserve, mlflow-kserve) behind proxy, first you need to configure `kserve-controller`
and `knative-serving` charms to function behind proxy.

> [!NOTE]  
> For information on how to fill out the proxy config values, see the `Running using Notebook > Prerequisites` section below.

1. Set the `http-proxy`, `https-proxy`, and `no-proxy` configs in `kserve-controller` charm
```
juju config kserve-controller http-proxy=<proxy_address>:<proxy_port> https-proxy=<proxy_address>:<proxy_port> no-proxy=<cluster cidr>,<service cluster ip range>,127.0.0.1,localhost,<nodes internal ip(s)>/24,<cluster hostname>,.svc,.local,.kubeflow
```

2. Set the `http-proxy`, `https-proxy`, and `no-proxy` configs in `knative-serving` charm
```
juju config knative-serving http-proxy=<proxy_address>:<proxy_port> https-proxy=<proxy_address>:<proxy_port> no-proxy=<cluster cidr>,<service cluster ip range>,127.0.0.1,localhost,<nodes internal ip(s)>/24,<cluster hostname>,.svc,.local
```

For Example:
```
juju config kserve-controller http-proxy=http://10.0.13.50:3128/ https-proxy=http://10.0.13.50:3128/ no-proxy=10.1.0.0/16,10.152.183.0/24,127.0.0.1,localhost,10.0.2.0/24,ip-10-0-2-157,.svc,.local,.kubeflow

juju config knative-serving http-proxy=http://10.0.13.50:3128/ https-proxy=http://10.0.13.50:3128/ no-proxy=10.1.0.0/16,10.152.183.0/24,127.0.0.1,localhost,10.0.2.0/24,ip-10-0-2-157,.svc,.local
```

#### Running using Notebook

##### Prerequisites

Edit the [PodDefault](tests/proxy-poddefault.yaml.j2) to replace the placeholders for:

* `http_proxy` and `https_proxy` - The address and port of your proxy server, format should be `<proxy_address>:<proxy_port>`
* `no_proxy` - A comma separated list of items that should not be proxied. It is recommended to include the following:

`<cluster cidr>,<service cluster ip range>,127.0.0.1,localhost,<nodes internal ip(s)>/24,<cluster hostname>,.svc,.local,.kubeflow`

where,

  * `<cluster cidr>`: you can get this value by running:

    ```
    cat /var/snap/microk8s/current/args/kube-proxy | grep cluster-cidr
    ```

  * `<service cluster ip range>`: you can get this value by running:

    ```
    cat /var/snap/microk8s/current/args/kube-apiserver | grep service-cluster-ip-range
    ```
   
  * `<nodes internal ip(s)>`: the Internal IP of the nodes where your cluster is running, you can get this value by running:

    ```
    microk8s kubectl get nodes -o wide
    ```
    It is the `INTERNAL-IP` value

  * `<hostname>`: the name of your host on which the cluster is deployed, you can use the `hostname` command to get it

  * `localhost` and `127.0.0.1` are recommended to avoid proxying requests to `localhost`

  * `.kubeflow`: is needed in the `no-proxy` values to allow communication with the minio service.


To run the tests behind proxy using Notebook:
1. Login to the Dashboard and Create a Profile
2. Apply the PodDefault to your Profile's namespace, make sure you already followed the Prerequisites
   section to modify the PodDefault. Apply it with:
   ```
   microk8s kubectl apply -f ./tests/proxy-poddefault.yaml -n <your_namespace>
   ```
3. Continue as described in the [Running inside a Notebook](#running-inside-a-notebook) section above.
   
   Currently, all tests are supported to run behind proxy except kfp-v1.

#### Running using `driver`

You can pass the `--proxy` flag and set the values for proxies to the tox command and this should automatically apply the required changes to run behind proxy.

```bash
tox -e kubeflow-<local|remote> -- --proxy http_proxy="http_proxy:port" https_proxy="https_proxy:port" no_proxy="<cluster cidr>,<service cluster ip range>,127.0.0.1,localhost,<nodes internal ip(s)>/24,<cluster hostname>,.svc,.local,.kubeflow"
```

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
