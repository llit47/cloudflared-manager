"""Safe presentation models for the dashboard."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from cloudflared_manager.cloudflared import (
    CloudflaredConfig,
    CloudflaredConfigError,
    parse_cloudflared_config,
)
from cloudflared_manager.config import Settings

StatusTone = Literal["neutral", "success", "error"]
ConfigLoader = Callable[[Path], CloudflaredConfig]


@dataclass(frozen=True, slots=True)
class StatusView:
    """Text and visual tone for one dashboard status."""

    label: str
    tone: StatusTone
    description: str


@dataclass(frozen=True, slots=True)
class DetectedRouteView:
    """Browser-safe presentation of a configured hostname route."""

    hostname: str
    path: str
    service: str
    status: str = "Configured · Read only"


@dataclass(frozen=True, slots=True)
class DashboardView:
    """All dynamic data required to render the dashboard."""

    tunnel: StatusView
    cloudflared: StatusView
    configuration: StatusView
    routes: tuple[DetectedRouteView, ...]
    empty_title: str
    empty_description: str

    @property
    def route_count(self) -> int:
        return len(self.routes)


def build_dashboard_view(
    settings: Settings,
    load_config: ConfigLoader = parse_cloudflared_config,
) -> DashboardView:
    """Load configured data and map it to safe dashboard presentation values."""

    cloudflared_status = StatusView(
        label="Not connected",
        tone="neutral",
        description="Service health checks are not available in this release.",
    )

    if settings.cloudflared_config_path is None:
        return DashboardView(
            tunnel=StatusView(
                label="Not configured",
                tone="neutral",
                description="No Cloudflare Tunnel configuration is selected.",
            ),
            cloudflared=cloudflared_status,
            configuration=StatusView(
                label="Not configured",
                tone="neutral",
                description="No cloudflared configuration source is selected.",
            ),
            routes=(),
            empty_title="No configuration selected",
            empty_description=(
                "Set an explicit cloudflared configuration path to detect ingress "
                "routes in read-only mode."
            ),
        )

    try:
        config = load_config(settings.cloudflared_config_path)
    except CloudflaredConfigError:
        return DashboardView(
            tunnel=StatusView(
                label="Unavailable",
                tone="error",
                description="Tunnel details could not be read from the configured file.",
            ),
            cloudflared=cloudflared_status,
            configuration=StatusView(
                label="Load error",
                tone="error",
                description=(
                    "The configured file could not be loaded. Check the server "
                    "configuration."
                ),
            ),
            routes=(),
            empty_title="Routes unavailable",
            empty_description=(
                "Detected routes will appear after the configuration error is resolved."
            ),
        )

    routes = tuple(
        DetectedRouteView(
            hostname=rule.hostname,
            path=rule.path or "All paths",
            service=_safe_service_target(rule.service),
        )
        for rule in config.hostname_routes
        if rule.hostname is not None
    )
    route_word = "route" if len(routes) == 1 else "routes"
    return DashboardView(
        tunnel=StatusView(
            label="Detected" if config.tunnel is not None else "Not declared",
            tone="success" if config.tunnel is not None else "neutral",
            description=(
                "A tunnel identifier is present in the configured file."
                if config.tunnel is not None
                else "The configured file does not declare a tunnel identifier."
            ),
        ),
        cloudflared=cloudflared_status,
        configuration=StatusView(
            label="Loaded",
            tone="success",
            description=f"{len(routes)} hostname {route_word} detected.",
        ),
        routes=routes,
        empty_title="No hostname routes detected",
        empty_description=(
            "The configuration loaded successfully but contains no hostname routes."
        ),
    )


def _safe_service_target(service: str) -> str:
    """Remove URL credentials, query parameters, and fragments before display."""

    try:
        parsed = urlsplit(service)
    except ValueError:
        return "Configured service"

    if parsed.netloc:
        safe_authority = parsed.netloc.rsplit("@", maxsplit=1)[-1]
        return urlunsplit((parsed.scheme, safe_authority, parsed.path, "", ""))

    return service.split("?", maxsplit=1)[0].split("#", maxsplit=1)[0]
