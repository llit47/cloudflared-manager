"""Read-only cloudflared configuration domain and parsing support."""

from cloudflared_manager.cloudflared.errors import (
    CloudflaredConfigError,
    ConfigFileNotFoundError,
    ConfigFileUnreadableError,
    ConfigInvalidYamlError,
    ConfigStructureError,
)
from cloudflared_manager.cloudflared.models import CloudflaredConfig, IngressRule
from cloudflared_manager.cloudflared.parser import parse_cloudflared_config

__all__ = [
    "CloudflaredConfig",
    "CloudflaredConfigError",
    "ConfigFileNotFoundError",
    "ConfigFileUnreadableError",
    "ConfigInvalidYamlError",
    "ConfigStructureError",
    "IngressRule",
    "parse_cloudflared_config",
]
