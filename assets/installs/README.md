# Configuring the test environment

While the provided K8s Job is designed to be standalone, it is often required to add extra
configuration to the workload responsible for running the UATs. This is the case, for instance,
when executing the tests behind a proxy; we should be able to inject environment variables into the
created Pod, in order to configure services running inside it to use the designated proxy. This is
possible by using a K8s [ConfigMap](https://kubernetes.io/docs/concepts/configuration/configmap/).

## Creating a ConfigMap

A ConfigMap is a K8s API object used to store non-confidential data in key-value pairs. The UATs
driver leverages this object to inject environment variables into the Pods deployed to run the
tests. More specifically, if a ConfigMap named `test-cm` exists when the Job is submitted (in the
same namespace, i.e. `test-kubeflow`), the environment variables specified in its `data` will be
propagated to the workload Pod. For instance, in order to add the `HTTP_PROXY` and `HTTPS_PROXY`
environment variables, you can apply an object like this (before running the tests):

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: test-cm  # do not edit
  namespace: test-kubeflow  # do not edit
data:
  # user-defined environment configuration
  HTTP_PROXY: http://squid.internal:3128
  HTTPS_PROXY: http://squid.internal:3128
```

## Using the ConfigMap template

For common cases- like working behind a proxy- you might want to use a template like the one
provided [here](./test-cm.yaml.j2), which you can render with the desired values using the Jinja
CLI.

### Installing the Jinja CLI

You can easily install the [Jinja CLI](https://pypi.org/project/jinja-cli/) through `pip`:

```shell
pip install jinja-cli
```

### Rendering the template

In many cases, the desired configuration is already set in environment variables locally. If those
match the names of the placeholders specified in the template, you can render it directly and store
the generated object in a YAML file:

```shell
jinja -E HTTP_PROXY -E HTTPS_PROXY test-cm.yaml.j2 -o test-cm.yaml
```

Then, you can apply the resulting ConfigMap to K8s using `kubectl`:

```shell
kubectl apply -f test-cm.yaml
```

For more options and information around templating you can consult the relevant
[documentation](https://pypi.org/project/jinja-cli/).
