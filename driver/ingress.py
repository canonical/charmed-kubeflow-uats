# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared ingress-gateway discovery helpers for the identity UAT suites.

These helpers locate the istio ingress ``Gateway`` serving a given domain and the
LoadBalancer IP of its backing Service, without hardcoding the charm/app name. They
are shared by the ``driver/m2m`` and ``driver/ui`` suites.
"""

import logging

from lightkube import Client
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.core_v1 import Service

log = logging.getLogger(__name__)

# Generic Gateway API resource, used to discover and patch the ingress Gateway.
GATEWAY_RESOURCE = create_namespaced_resource(
    group="gateway.networking.k8s.io",
    version="v1",
    kind="Gateway",
    plural="gateways",
)


def get_service_lb_ip(client: Client, namespace: str, service: str) -> str:
    """Return the LoadBalancer IP of a Kubernetes Service."""
    svc = client.get(Service, name=service, namespace=namespace)
    ingress = (svc.status.loadBalancer.ingress or []) if svc.status else []
    assert ingress, f"Service {namespace}/{service} has no LoadBalancer IP yet"
    ip = ingress[0].ip
    assert ip, f"Service {namespace}/{service} has no LoadBalancer IP yet"
    return ip


def find_gateway_for_domain(client: Client, namespace: str, domain: str) -> str:
    """Return the name of the istio Gateway serving the given domain.

    The Gateway is identified by a listener whose hostname matches ``domain`` (either
    exactly or as a wildcard subdomain), so the charm/app name does not need to be
    known in advance.

    Args:
        client: The lightkube client to use.
        namespace: The namespace to look for Gateways in.
        domain: The domain the Gateway should serve, e.g. ``api.kubeflow.com``.

    Returns:
        The name of the matching Gateway (equal to the charm app name).
    """
    for gateway in client.list(GATEWAY_RESOURCE, namespace=namespace):
        spec = gateway.spec or {}
        if spec.get("gatewayClassName") != "istio":
            continue
        for listener in spec.get("listeners", []):
            hostname = listener.get("hostname", "") or ""
            if hostname == domain or hostname.endswith(f".{domain}"):
                name = gateway.metadata.name
                log.info(f"Discovered istio Gateway '{name}' serving domain {domain}")
                return name
    raise AssertionError(
        f"No istio Gateway serving domain {domain} found in namespace {namespace}"
    )


def gateway_proxy_name(gateway: str) -> str:
    """Return the istio proxy resource name for a Gateway.

    The istio-ingress-k8s charm names the ServiceAccount, the LoadBalancer Service,
    and the Deployment after the app (``<gateway>-istio``). This single name is used
    both as the Service name for LB IP lookups and as the ServiceAccount name in
    istio principal strings.
    """
    return f"{gateway}-istio"
