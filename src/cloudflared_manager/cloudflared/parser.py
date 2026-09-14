"""Strict, read-only parsing of an explicitly selected cloudflared YAML file."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from cloudflared_manager.cloudflared.errors import (
    ConfigFileNotFoundError,
    ConfigFileUnreadableError,
    ConfigInvalidYamlError,
    ConfigStructureError,
)
from cloudflared_manager.cloudflared.models import CloudflaredConfig, IngressRule


def parse_cloudflared_config(path: str | Path) -> CloudflaredConfig:
    """Read and parse ``path`` without mutating files or external systems."""

    config_path = Path(path)
    try:
        contents = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ConfigFileNotFoundError(
            "Cloudflared configuration file was not found."
        ) from error
    except (OSError, UnicodeError) as error:
        raise ConfigFileUnreadableError(
            "Cloudflared configuration file could not be read."
        ) from error

    try:
        document = yaml.safe_load(contents)
    except yaml.YAMLError as error:
        raise ConfigInvalidYamlError(
            "Cloudflared configuration contains invalid YAML."
        ) from error

    if not isinstance(document, Mapping):
        raise ConfigStructureError(
            "Cloudflared configuration root must be a mapping."
        )

    tunnel = _optional_string(document, "tunnel", "Tunnel")
    ingress = document.get("ingress")
    if not isinstance(ingress, list) or not ingress:
        raise ConfigStructureError(
            "Cloudflared configuration must define 'ingress' as a non-empty list."
        )

    rules = tuple(
        _parse_ingress_rule(entry, position, len(ingress))
        for position, entry in enumerate(ingress, start=1)
    )
    if not rules[-1].is_catch_all:
        raise ConfigStructureError(
            "The final ingress rule must be a catch-all without hostname or path."
        )

    return CloudflaredConfig(tunnel=tunnel, ingress_rules=rules)


def _parse_ingress_rule(
    entry: Any,
    position: int,
    rule_count: int,
) -> IngressRule:
    if not isinstance(entry, Mapping):
        raise ConfigStructureError(
            f"Ingress rule at position {position} must be a mapping."
        )

    service = _required_string(entry, "service", position)
    hostname = _optional_string(entry, "hostname", "Ingress hostname", position)
    path = _optional_string(entry, "path", "Ingress path", position)
    is_catch_all = hostname is None and path is None

    if is_catch_all and position != rule_count:
        raise ConfigStructureError(
            f"Catch-all ingress rule at position {position} must be last."
        )

    return IngressRule(
        hostname=hostname,
        path=path,
        service=service,
        is_catch_all=is_catch_all,
    )


def _required_string(entry: Mapping[Any, Any], key: str, position: int) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigStructureError(
            f"Ingress rule at position {position} must define a non-empty '{key}'."
        )
    return value


def _optional_string(
    entry: Mapping[Any, Any],
    key: str,
    label: str,
    position: int | None = None,
) -> str | None:
    if key not in entry:
        return None

    value = entry[key]
    if isinstance(value, str) and value.strip():
        return value

    location = f" at ingress position {position}" if position is not None else ""
    raise ConfigStructureError(
        f"{label}{location} must be a non-empty string when provided."
    )
