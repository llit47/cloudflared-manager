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
    CloudflaredRuntime,
    ManagementMode,
    RuntimeDiscoveryError,
    parse_cloudflared_config,
)
from cloudflared_manager.cloudflared.discovery import (
    RuntimeDiscoveryProvider,
    discover_cloudflared,
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
    discover_runtime: RuntimeDiscoveryProvider = discover_cloudflared,
) -> DashboardView:
    """Load configured data and map it to safe dashboard presentation values."""

    cloudflared_status = _cloudflared_status(settings, discover_runtime)

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


def _cloudflared_status(
    settings: Settings,
    discover_runtime: RuntimeDiscoveryProvider,
) -> StatusView:
    if not settings.runtime_discovery_enabled:
        return StatusView(
            label="Not connected",
            tone="neutral",
            description="Runtime discovery is disabled; local service state is unknown.",
        )

    try:
        runtime = discover_runtime(True)
    except RuntimeDiscoveryError:
        return StatusView(
            label="Unavailable",
            tone="error",
            description="Local cloudflared runtime inspection could not be completed.",
        )

    if runtime is None:
        return StatusView(
            label="Unavailable",
            tone="error",
            description="Local cloudflared runtime information is unavailable.",
        )

    label, tone = _runtime_label(runtime)
    return StatusView(
        label=label,
        tone=tone,
        description=" ".join(_runtime_description(runtime)),
    )


def _runtime_label(runtime: CloudflaredRuntime) -> tuple[str, StatusTone]:
    if runtime.active_state == "failed":
        return "Failed", "error"
    if (
        runtime.service_exists is True
        and runtime.active_state == "active"
        and runtime.sub_state == "running"
    ):
        return "Running", "success"
    if runtime.service_exists and runtime.active_state == "inactive":
        return "Stopped", "neutral"
    if runtime.service_exists:
        return "Detected", "neutral"
    if runtime.executable_exists:
        return "Installed", "neutral"
    if not runtime.systemd_available:
        return "Unavailable", "neutral"
    return "Not detected", "neutral"


def _runtime_description(runtime: CloudflaredRuntime) -> list[str]:
    facts: list[str] = []
    if runtime.executable_exists:
        version = f" version {runtime.version}" if runtime.version else ""
        facts.append(f"cloudflared{version} is installed.")
    else:
        facts.append("The cloudflared executable was not detected.")

    if not runtime.systemd_available:
        facts.append("systemd inspection is unavailable.")
    elif runtime.service_exists is False:
        facts.append("cloudflared.service is not loaded.")
    elif runtime.service_exists is None:
        facts.append("cloudflared.service state could not be determined.")
    else:
        state = "/".join(
            value
            for value in (runtime.active_state, runtime.sub_state)
            if value is not None
        )
        facts.append(
            f"cloudflared.service is {state}."
            if state
            else "cloudflared.service is loaded."
        )
        if runtime.enabled_state is not None:
            facts.append(f"Startup state: {runtime.enabled_state}.")
        if runtime.main_pid is not None:
            facts.append(f"Main process ID: {runtime.main_pid}.")

    facts.append(_management_mode_description(runtime.management_mode))
    return facts


def _management_mode_description(mode: ManagementMode) -> str:
    if mode is ManagementMode.LOCAL_CONFIG:
        return "Local configuration mode detected."
    if mode is ManagementMode.REMOTE_TOKEN:
        return "Token-managed mode detected."
    return "Management mode could not be determined."


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
