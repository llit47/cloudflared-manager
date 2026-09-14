"""Typed domain models for the cloudflared data currently consumed."""

from __future__ import annotations

from dataclasses import dataclass


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
