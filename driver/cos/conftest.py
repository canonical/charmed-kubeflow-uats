# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Conftest for COS (observability) integration tests.

Holds the fixtures shared by every COS suite: the Jubilant handle to the
COS model, and the URLs (and credentials) of the COS Lite applications the suites query.
Product-specific fixtures and expectations live in each test module.
"""

import sys
from pathlib import Path

import jubilant
import pytest

# Add the driver directory to path so shared modules are importable.
sys.path.insert(0, str(Path(__file__).parent.parent))

from cos.helpers import (  # noqa: E402  (import must follow the sys.path setup above)
    GRAFANA_APP,
    LOKI_APP,
    PROMETHEUS_APP,
    get_cos_app_url,
    get_grafana_admin_password,
    get_proxied_endpoints,
)


@pytest.fixture(scope="module")
def cos_model(request: pytest.FixtureRequest) -> str:
    """Name of the Juju model where COS Lite is deployed."""
    return request.config.getoption("--cos-model")


@pytest.fixture(scope="module")
def cos_juju(cos_model: str) -> jubilant.Juju:
    """A Jubilant handle to the COS model."""
    return jubilant.Juju(model=cos_model)


@pytest.fixture(scope="module")
def proxied_endpoints(cos_juju: jubilant.Juju) -> dict[str, dict]:
    """The endpoints proxied by Traefik in the COS model."""
    return get_proxied_endpoints(cos_juju)


@pytest.fixture(scope="module")
def prometheus_url(proxied_endpoints: dict[str, dict], cos_model: str) -> str:
    """URL of Prometheus, through Traefik."""
    return get_cos_app_url(proxied_endpoints, cos_model, PROMETHEUS_APP)


@pytest.fixture(scope="module")
def loki_url(proxied_endpoints: dict[str, dict], cos_model: str) -> str:
    """URL of Loki, through Traefik."""
    return get_cos_app_url(proxied_endpoints, cos_model, LOKI_APP)


@pytest.fixture(scope="module")
def grafana_url(proxied_endpoints: dict[str, dict], cos_model: str) -> str:
    """URL of Grafana, through Traefik."""
    return get_cos_app_url(proxied_endpoints, cos_model, GRAFANA_APP)


@pytest.fixture(scope="module")
def grafana_admin_password(cos_juju: jubilant.Juju) -> str:
    """Password of the Grafana admin user."""
    return get_grafana_admin_password(cos_juju)
