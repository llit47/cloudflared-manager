"""Bounded local hostname ingress values and stale route selectors."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Mapping

from cloudflared_manager.cloudflared.editing.errors import MutationRejectedError

_HOSTNAME = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://[^\s]+$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class LocalRoute:
    hostname: str
    path: str | None
    service: str

    def __post_init__(self) -> None:
        if type(self.hostname) is not str or not _HOSTNAME.fullmatch(self.hostname):
            raise MutationRejectedError("Invalid local ingress hostname.")
        if self.path is not None:
            if type(self.path) is not str or not self.path.startswith("/") or len(self.path) > 256 or _has_control(self.path):
                raise MutationRejectedError("Invalid local ingress path.")
        if (type(self.service) is not str or not 1 <= len(self.service) <= 512
            or _has_control(self.service) or self.service != self.service.strip()
            or not (_SCHEME.fullmatch(self.service) or re.fullmatch(r"http_status:[1-5][0-9]{2}", self.service))):
            raise MutationRejectedError("Invalid local ingress service.")


@dataclass(frozen=True, slots=True)
class RouteSelector:
    position: int
    fingerprint: str

    def __post_init__(self) -> None:
        if type(self.position) is not int or not 0 <= self.position <= 4095:
            raise MutationRejectedError("Invalid local ingress selector.")
        if type(self.fingerprint) is not str or not _DIGEST.fullmatch(self.fingerprint):
            raise MutationRejectedError("Invalid local ingress selector.")


def require_revision(value: str) -> str:
    if type(value) is not str or not _DIGEST.fullmatch(value):
        raise MutationRejectedError("Invalid source revision.")
    return value


def route_fingerprint(rule: Mapping[str, object]) -> str:
    """Fingerprint the selected route projection; source digest covers all YAML."""
    projection = {key: rule.get(key) for key in ("hostname", "path", "service")}
    if not isinstance(projection["hostname"], str) or not isinstance(projection["service"], str):
        raise MutationRejectedError("The selected ingress route is unsupported.")
    encoded = json.dumps(projection, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)
