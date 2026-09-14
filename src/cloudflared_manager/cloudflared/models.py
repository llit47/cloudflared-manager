"""Typed domain models for the cloudflared data currently consumed."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


@dataclass(frozen=True, slots=True)
class IngressRule:
    """One ordered ingress rule from a cloudflared configuration."""

    service: str
    hostname: str | None = None
    path: str | None = None
    is_catch_all: bool = False


@dataclass(frozen=True, slots=True)
class CloudflaredConfig:
    """The small read-only configuration projection needed by the manager."""

    tunnel: str | None
    ingress_rules: tuple[IngressRule, ...]

    @property
    def hostname_routes(self) -> tuple[IngressRule, ...]:
        """Return user-visible hostname rules without the terminal fallback."""

        return tuple(
            rule
            for rule in self.ingress_rules
            if rule.hostname is not None and not rule.is_catch_all
        )


class ManagementMode(StrEnum):
    """How the observed cloudflared service appears to be managed."""

    LOCAL_CONFIG = "local_config"
    REMOTE_TOKEN = "remote_token"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CloudflaredRuntime:
    """Sanitized facts discovered from the local executable and systemd."""

    executable_path: Path | None
    version: str | None
    systemd_available: bool
    service_exists: bool | None
    load_state: str | None
    active_state: str | None
    sub_state: str | None
    enabled_state: str | None
    main_pid: int | None
    management_mode: ManagementMode
    explicit_config_path: Path | None

    @property
    def executable_exists(self) -> bool:
        """Return whether a verified executable path was discovered."""

        return self.executable_path is not None
