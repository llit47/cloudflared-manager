"""Validation for deployment inputs that can influence root operations."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence

from cloudflared_manager.deployment.errors import ValidationError

_SHA = re.compile(r"^[0-9a-f]{40}$")
_INTERFACE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")
_RFC1918 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
MINIMUM_PYTHON = (3, 12)
MINIMUM_UNPRIVILEGED_PORT = 1024


def validate_sha(value: str) -> str:
    """Return a normalized exact Git commit SHA or reject it."""

    normalized = value.strip().lower()
    if _SHA.fullmatch(normalized) is None:
        raise ValidationError("The release revision is not a 40-character Git SHA.")
    return normalized


def validate_python_version(version: Sequence[int]) -> tuple[int, int]:
    """Accept a Python interpreter only when it satisfies the project floor."""

    if len(version) < 2:
        raise ValidationError("The Python interpreter version is unavailable.")
    parsed = (int(version[0]), int(version[1]))
    if parsed < MINIMUM_PYTHON:
        raise ValidationError("Cloudflared Manager requires Python 3.12 or newer.")
    return parsed


def validate_bind_host(value: str) -> str:
    """Validate a concrete RFC1918 IPv4 manager bind address."""

    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError as error:
        raise ValidationError("The bind address must be a valid IPv4 address.") from error

    if not isinstance(address, ipaddress.IPv4Address):
        raise ValidationError("The bind address must be IPv4 in this release.")
    if not any(address in network for network in _RFC1918):
        raise ValidationError("The bind address must be a concrete RFC1918 LAN address.")
    if address.is_unspecified or address.is_loopback or address.is_link_local:
        raise ValidationError("The bind address is not suitable for LAN access.")
    if address.is_multicast or address.is_reserved:
        raise ValidationError("The bind address is not suitable for LAN access.")
    return str(address)


def validate_port(value: str | int) -> int:
    """Validate a TCP port usable by the unprivileged service account."""

    try:
        port = int(value)
    except (TypeError, ValueError) as error:
        raise ValidationError("The bind port must be an integer.") from error
    if str(port) != str(value).strip() and not isinstance(value, int):
        raise ValidationError("The bind port must be a plain decimal integer.")
    if not MINIMUM_UNPRIVILEGED_PORT <= port <= 65_535:
        raise ValidationError("The bind port must be between 1024 and 65535.")
    return port


def validate_interface(value: str) -> str:
    """Validate an interface name obtained from the local routing table."""

    if _INTERFACE.fullmatch(value) is None:
        raise ValidationError("The default-route interface name is invalid.")
    return value
