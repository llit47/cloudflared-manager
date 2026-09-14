"""Read-only cloudflared configuration domain and parsing support."""

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
from cloudflared_manager.cloudflared.parser import parse_cloudflared_config

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
