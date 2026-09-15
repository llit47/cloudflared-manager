"""Validated production settings derived from the manager EnvironmentFile."""

from __future__ import annotations

from dataclasses import dataclass

from cloudflared_manager.deployment.environment import EnvironmentDocument
from cloudflared_manager.deployment.errors import HostOperationError
from cloudflared_manager.deployment.validation import validate_bind_host, validate_port


@dataclass(frozen=True, slots=True)
class ManagerSettings:
    bind_host: str
    bind_port: int
    runtime_discovery_enabled: bool


def complete_existing_environment(
    document: EnvironmentDocument,
    bind_host: str,
    bind_port: int,
) -> EnvironmentDocument:
    values = document.managed_values()
    defaults = {
        "CFM_APP_NAME": "cloudflared-manager",
        "CFM_MODE": "production",
        "CFM_BIND_HOST": bind_host,
        "CFM_BIND_PORT": str(bind_port),
        "CFM_RUNTIME_DISCOVERY_ENABLED": "true",
    }
    return document.updated({key: value for key, value in defaults.items() if key not in values})


def settings_from_document(document: EnvironmentDocument) -> ManagerSettings:
    values = document.managed_values()
    required = {
        "CFM_BIND_HOST",
        "CFM_BIND_PORT",
        "CFM_RUNTIME_DISCOVERY_ENABLED",
    }
    if not required.issubset(values):
        raise HostOperationError("The manager environment file is missing required settings.")
    if values.get("CFM_MODE") != "production":
        raise HostOperationError("The manager environment must use production mode.")
    if values.get("CFM_APP_NAME") != "cloudflared-manager":
        raise HostOperationError("The manager environment has an unsupported application name.")
    discovery = values["CFM_RUNTIME_DISCOVERY_ENABLED"].lower()
    if discovery not in {"true", "false"}:
        raise HostOperationError("The runtime discovery setting must be true or false.")
    return ManagerSettings(
        bind_host=validate_bind_host(values["CFM_BIND_HOST"]),
        bind_port=validate_port(values["CFM_BIND_PORT"]),
        runtime_discovery_enabled=discovery == "true",
    )
