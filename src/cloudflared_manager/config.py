"""Typed application configuration with safe local defaults."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from cloudflared_manager.runtime_identity import runtime_config_id

ApplicationMode = Literal["development", "test", "production"]


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings supplied directly or through ``CFM_*`` variables."""

    app_name: str = "Cloudflared Manager"
    mode: ApplicationMode = "development"
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    cloudflared_config_path: Path | None = None
    runtime_discovery_enabled: bool = False

    @property
    def config_id(self) -> str:
        return runtime_config_id(
            self.bind_host,
            self.bind_port,
            self.runtime_discovery_enabled,
            self.cloudflared_config_path,
        )

    def __post_init__(self) -> None:
        if not self.app_name.strip():
            raise ValueError("app_name must not be empty")
        if self.mode not in {"development", "test", "production"}:
            raise ValueError("mode must be development, test, or production")
        if not self.bind_host.strip():
            raise ValueError("bind_host must not be empty")
        if not 1 <= self.bind_port <= 65_535:
            raise ValueError("bind_port must be between 1 and 65535")
        if not isinstance(self.runtime_discovery_enabled, bool):
            raise ValueError("runtime_discovery_enabled must be a boolean")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Build settings from environment variables without reading files."""

        values = os.environ if environ is None else environ
        mode = values.get("CFM_MODE", "development").strip().lower()
        port_value = values.get("CFM_BIND_PORT", "8000").strip()
        config_path = values.get("CFM_CLOUDFLARED_CONFIG_PATH", "").strip()
        discovery_value = values.get("CFM_RUNTIME_DISCOVERY_ENABLED", "false")

        try:
            bind_port = int(port_value)
        except ValueError as error:
            raise ValueError("CFM_BIND_PORT must be an integer") from error

        return cls(
            app_name=values.get("CFM_APP_NAME", "Cloudflared Manager").strip(),
            mode=cast(ApplicationMode, mode),
            bind_host=values.get("CFM_BIND_HOST", "127.0.0.1").strip(),
            bind_port=bind_port,
            cloudflared_config_path=Path(config_path) if config_path else None,
            runtime_discovery_enabled=_strict_boolean(
                discovery_value,
                "CFM_RUNTIME_DISCOVERY_ENABLED",
            ),
        )


def _strict_boolean(value: str, variable: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{variable} must be true or false")
