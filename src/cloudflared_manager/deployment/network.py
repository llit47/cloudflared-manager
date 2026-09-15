"""Conservative selection of a concrete private LAN bind address."""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from cloudflared_manager.deployment.errors import NetworkSelectionError, ValidationError
from cloudflared_manager.deployment.validation import validate_bind_host, validate_interface


class NetworkInspector(Protocol):
    """Provide fixed read-only `ip` command observations."""

    def default_routes(self) -> str:
        """Return `ip -4 route show default` output."""

    def global_addresses(self, interface: str) -> str:
        """Return global IPv4 address output for one validated interface."""

    def all_global_addresses(self) -> str:
        """Return global IPv4 address output for all local interfaces."""


@dataclass(frozen=True, slots=True)
class DefaultRoute:
    interface: str
    metric: int


def select_lan_address(
    inspector: NetworkInspector,
    explicit_override: str | None = None,
) -> str:
    """Select one high-confidence RFC1918 address or fail without guessing."""

    if explicit_override is not None and explicit_override.strip():
        return validate_local_bind_address(inspector, explicit_override)

    routes = parse_default_routes(inspector.default_routes())
    if not routes:
        raise NetworkSelectionError(
            "No normal IPv4 default-route interface could be determined. "
            "Set CFM_INSTALL_BIND_HOST to a concrete RFC1918 address."
        )

    lowest_metric = min(route.metric for route in routes)
    interfaces = {
        route.interface for route in routes if route.metric == lowest_metric
    }
    if len(interfaces) != 1:
        raise NetworkSelectionError(
            "Multiple equally preferred IPv4 default-route interfaces were found. "
            "Set CFM_INSTALL_BIND_HOST explicitly."
        )

    interface = next(iter(interfaces))
    candidates = parse_global_addresses(
        inspector.global_addresses(interface),
        interface,
    )
    if not candidates:
        raise NetworkSelectionError(
            "The default-route interface has no suitable RFC1918 address. "
            "Set CFM_INSTALL_BIND_HOST explicitly."
        )
    if len(candidates) > 1:
        raise NetworkSelectionError(
            "The default-route interface has multiple suitable LAN addresses. "
            "Set CFM_INSTALL_BIND_HOST explicitly."
        )
    return candidates[0]


def validate_local_bind_address(inspector: NetworkInspector, value: str) -> str:
    """Require a suitable RFC1918 address that is currently assigned locally."""

    requested = validate_bind_host(value)
    assigned = parse_all_global_addresses(inspector.all_global_addresses())
    if requested not in assigned:
        raise NetworkSelectionError(
            "The requested bind address is not assigned to a local global IPv4 interface."
        )
    return requested


def parse_default_routes(output: str) -> tuple[DefaultRoute, ...]:
    """Parse only the interface and metric from machine-local route output."""

    routes: list[DefaultRoute] = []
    for line in output[:65_536].splitlines():
        fields = line.split()
        if not fields or fields[0] != "default" or "dev" not in fields:
            continue
        try:
            interface = validate_interface(fields[fields.index("dev") + 1])
        except (IndexError, ValidationError):
            continue
        metric = 0
        if "metric" in fields:
            try:
                metric = int(fields[fields.index("metric") + 1])
            except (IndexError, ValueError):
                continue
            if metric < 0:
                continue
        routes.append(DefaultRoute(interface=interface, metric=metric))
    return tuple(routes)


def parse_global_addresses(output: str, interface: str) -> tuple[str, ...]:
    """Return unique RFC1918 addresses observed on the selected interface."""

    expected_interface = validate_interface(interface)
    addresses: list[str] = []
    for line in output[:65_536].splitlines():
        fields = line.split()
        if "inet" not in fields:
            continue
        try:
            observed_interface = fields[1].rstrip(":").split("@", 1)[0]
            value = fields[fields.index("inet") + 1].split("/", 1)[0]
        except (IndexError, ValueError):
            continue
        if observed_interface != expected_interface:
            continue
        try:
            address = validate_bind_host(value)
        except ValidationError:
            continue
        if address not in addresses:
            addresses.append(address)
    return tuple(addresses)


def parse_all_global_addresses(output: str) -> tuple[str, ...]:
    """Return unique suitable RFC1918 addresses assigned to any local interface."""

    addresses: list[str] = []
    for line in output[:65_536].splitlines():
        fields = line.split()
        if len(fields) < 6 or "inet" not in fields or "scope" not in fields:
            continue
        try:
            interface = validate_interface(fields[1].rstrip(":").split("@", 1)[0])
            scope = fields[fields.index("scope") + 1]
            value = fields[fields.index("inet") + 1].split("/", 1)[0]
        except (IndexError, ValueError, ValidationError):
            continue
        if scope != "global" or _is_unsuitable_explicit_interface(interface):
            continue
        try:
            address = validate_bind_host(value)
        except ValidationError:
            continue
        if address not in addresses:
            addresses.append(address)
    return tuple(addresses)


def _is_unsuitable_explicit_interface(interface: str) -> bool:
    """Exclude obvious local-only bridge/loopback interfaces from LAN overrides."""

    return interface == "lo" or interface.startswith(
        ("docker", "veth", "virbr", "podman", "br-")
    )
