"""Read-only cloudflared configuration domain and parsing support."""

from pathlib import Path

from cloudflared_manager.cloudflared.errors import (
    CloudflaredConfigError,
    CommandExecutionError,
    CommandTimedOutError,
    CommandUnavailableError,
    ConfigFileNotFoundError,
    ConfigFileUnreadableError,
    ConfigInvalidYamlError,
    ConfigStructureError,
    RuntimeDiscoveryError,
)
from cloudflared_manager.cloudflared.models import (
    CloudflaredConfig,
    CloudflaredRuntime,
    IngressRule,
    ManagementMode,
)


def parse_cloudflared_config(path: str | Path) -> CloudflaredConfig:
    """Load the YAML parser only when configuration parsing is requested."""

    from cloudflared_manager.cloudflared.parser import parse_cloudflared_config as parse

    return parse(path)

__all__ = [
    "CloudflaredConfig",
    "CloudflaredConfigError",
    "CloudflaredRuntime",
    "CommandExecutionError",
    "CommandTimedOutError",
    "CommandUnavailableError",
    "ConfigFileNotFoundError",
    "ConfigFileUnreadableError",
    "ConfigInvalidYamlError",
    "ConfigStructureError",
    "IngressRule",
    "ManagementMode",
    "RuntimeDiscoveryError",
    "parse_cloudflared_config",
]
