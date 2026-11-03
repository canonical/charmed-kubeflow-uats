# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared ingress-gateway discovery helpers for the UAT suites.

These helpers locate the istio ingress ``Gateway`` KServe attaches its routes to, without
hardcoding the charm/app name, and patch its listeners. They are used by the root
``patch_gateway`` fixture.
"""

import json
import logging
from typing import Optional

from lightkube import ApiError, Client
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.core_v1 import ConfigMap
from lightkube.types import PatchType

log = logging.getLogger(__name__)

# ConfigMap where kserve-controller publishes its ingress configuration.
KSERVE_CONFIGMAP = "inferenceservice-config"

# Generic Gateway API resource, used to discover and patch the ingress Gateway.
GATEWAY_RESOURCE = create_namespaced_resource(
    group="gateway.networking.k8s.io",
    version="v1",
    kind="Gateway",
    plural="gateways",
)


def get_kserve_gateway(client: Client, namespace: str) -> Optional[tuple[str, str]]:
    """Return the ``(namespace, name)`` of the Gateway KServe attaches its routes to.

    Read from the ``ingress`` entry of the ``inferenceservice-config`` ConfigMap that
    kserve-controller renders from its ``gateway-metadata`` relation.

    Args:
        client: The lightkube client to use.
        namespace: The namespace where kserve-controller is deployed.

    Returns:
        The Gateway ``(namespace, name)``, or ``None`` if KServe is not deployed or
        does not use the Gateway API (e.g. Knative/sidecar deployments).
    """
    try:
        configmap = client.get(ConfigMap, name=KSERVE_CONFIGMAP, namespace=namespace)
    except ApiError as error:
        if error.status.code == 404:
            return None
        raise

    ingress = json.loads((configmap.data or {}).get("ingress") or "{}")
    if not ingress.get("enableGatewayApi"):
        return None

    gateway_namespace, _, gateway_name = ingress.get("kserveIngressGateway", "").partition("/")
    if not (gateway_namespace and gateway_name):
        return None
    return gateway_namespace, gateway_name


def remove_gateway_listener_hostnames(
    client: Client, namespace: str, gateway: str
) -> dict[str, str]:
    """Remove the hostname from every listener of a Gateway, so it accepts any route.

    Workaround for https://github.com/canonical/kserve-operators/issues/491 (see also
    https://github.com/canonical/service-mesh/issues/102): the istio-ingress-k8s charm
    pins each listener to a single exact hostname (``external_hostname``, or the
    LoadBalancer hostname when unset, e.g. on EKS), which rejects KServe's
    per-InferenceService subdomain routes with ``NoMatchingListenerHostname``. Without a
    hostname the listener behaves as on clusters whose LoadBalancer only exposes an IP.
    Remove this once the charm supports wildcard listeners natively.

    Returns:
        The removed hostnames keyed by listener name, to be passed to
        ``restore_gateway_listener_hostnames``.
    """
    gw = client.get(GATEWAY_RESOURCE, name=gateway, namespace=namespace)
    listeners = (gw.spec or {}).get("listeners", [])
    removed = {
        listener["name"]: listener["hostname"]
        for listener in listeners
        if listener.get("hostname")
    }
    patch = [
        {"op": "remove", "path": f"/spec/listeners/{index}/hostname"}
        for index, listener in enumerate(listeners)
        if listener.get("hostname")
    ]
    if not patch:
        log.info(f"Gateway {namespace}/{gateway} listeners have no hostname, nothing to patch")
        return {}
    log.info(f"Removing the listener hostnames of Gateway {namespace}/{gateway}: {removed}")
    client.patch(GATEWAY_RESOURCE, gateway, patch, namespace=namespace, patch_type=PatchType.JSON)
    return removed


def restore_gateway_listener_hostnames(
    client: Client, namespace: str, gateway: str, hostnames: dict[str, str]
) -> None:
    """Restore the listener hostnames removed by ``remove_gateway_listener_hostnames``.

    Listeners are matched by name, and only those still without a hostname are
    restored, so changes made in the meantime (e.g. by the charm reconciling the
    Gateway) are not overwritten.

    Args:
        client: The lightkube client to use.
        namespace: The namespace of the Gateway.
        gateway: The name of the Gateway.
        hostnames: The removed hostnames keyed by listener name.
    """
    if not hostnames:
        return
    gw = client.get(GATEWAY_RESOURCE, name=gateway, namespace=namespace)
    listeners = (gw.spec or {}).get("listeners", [])
    patch = [
        {
            "op": "add",
            "path": f"/spec/listeners/{index}/hostname",
            "value": hostnames[listener["name"]],
        }
        for index, listener in enumerate(listeners)
        if listener.get("name") in hostnames and not listener.get("hostname")
    ]
    if not patch:
        log.info(f"Gateway {namespace}/{gateway} listener hostnames already restored")
        return
    log.info(f"Restoring the listener hostnames of Gateway {namespace}/{gateway}")
    client.patch(GATEWAY_RESOURCE, gateway, patch, namespace=namespace, patch_type=PatchType.JSON)
