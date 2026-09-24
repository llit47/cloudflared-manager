"""Independent, bounded protocol for three local ingress operations only."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from cloudflared_manager.cloudflared.editing.local_ingress import (
    LocalRoute, RouteSelector, require_revision,
)
from cloudflared_manager.cloudflared.editing.errors import MutationRejectedError

VERSION = 1
MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 512
Action = Literal["local_ingress_add", "local_ingress_edit", "local_ingress_delete"]
RESULT_CODES = frozenset({
    "CHANGED", "NO_CHANGE", "STALE_CONFLICT", "INVALID_REQUEST", "INVALID_DOMAIN_DATA",
    "BUSY", "UNSUPPORTED_CONFIG", "VALIDATION_FAILED", "PRIVILEGED_BOUNDARY_UNAVAILABLE",
    "ACTIVATION_FAILED_ROLLED_BACK", "RECOVERY_REQUIRED", "ACTIVATION_FAILED",
})


class MutationProtocolRefused(Exception):
    def __init__(self, code: str = "INVALID_REQUEST") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MutationRequest:
    action: Action
    source_revision: str
    route: LocalRoute | None = None
    target: RouteSelector | None = None

    def __post_init__(self) -> None:
        if self.action not in ("local_ingress_add", "local_ingress_edit", "local_ingress_delete"):
            raise MutationProtocolRefused()
        if (self.route is not None and type(self.route) is not LocalRoute) or (self.target is not None and type(self.target) is not RouteSelector):
            raise MutationProtocolRefused()
        try:
            require_revision(self.source_revision)
        except MutationRejectedError:
            raise MutationProtocolRefused() from None
        if ((self.action == "local_ingress_add" and (self.route is None or self.target is not None))
            or (self.action == "local_ingress_edit" and (self.route is None or self.target is None))
            or (self.action == "local_ingress_delete" and (self.route is not None or self.target is None))):
            raise MutationProtocolRefused()


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MutationProtocolRefused()
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise MutationProtocolRefused()


def parse_request(raw: bytes) -> MutationRequest:
    if type(raw) is not bytes or not raw or len(raw) > MAX_REQUEST_BYTES:
        raise MutationProtocolRefused()
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs,
                         parse_constant=_reject_constant)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise MutationProtocolRefused() from None
    if type(obj) is not dict:
        raise MutationProtocolRefused()
    if type(obj.get("version")) is not int or obj["version"] != VERSION:
        raise MutationProtocolRefused()
    action = obj.get("action")
    if type(action) is not str or action not in ("local_ingress_add", "local_ingress_edit", "local_ingress_delete"):
        raise MutationProtocolRefused()
    expected = {"version", "action", "source_revision"}
    if action != "local_ingress_delete":
        expected.add("route")
    if action != "local_ingress_add":
        expected.add("target")
    if set(obj) != expected:
        raise MutationProtocolRefused()
    try:
        revision = require_revision(obj["source_revision"])
        route = None
        target = None
        if "route" in obj:
            values = obj["route"]
            if type(values) is not dict or set(values) != {"hostname", "path", "service"}:
                raise MutationProtocolRefused()
            route = LocalRoute(values["hostname"], values["path"], values["service"])
        if "target" in obj:
            values = obj["target"]
            if type(values) is not dict or set(values) != {"position", "fingerprint"}:
                raise MutationProtocolRefused()
            target = RouteSelector(values["position"], values["fingerprint"])
        return MutationRequest(action, revision, route, target)
    except (MutationRejectedError, KeyError, TypeError):
        raise MutationProtocolRefused() from None


def encode_request(request: MutationRequest) -> bytes:
    obj: dict[str, object] = {"version": VERSION, "action": request.action,
                              "source_revision": request.source_revision}
    if request.route is not None:
        obj["route"] = {"hostname": request.route.hostname, "path": request.route.path,
                        "service": request.route.service}
    if request.target is not None:
        obj["target"] = {"position": request.target.position,
                         "fingerprint": request.target.fingerprint}
    raw = (json.dumps(obj, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
    parse_request(raw)
    return raw


def encode_response(*, ok: bool, code: str) -> bytes:
    if type(ok) is not bool or code not in RESULT_CODES:
        raise MutationProtocolRefused()
    raw = (json.dumps({"version": VERSION, "ok": ok, "code": code},
                      separators=(",", ":"), sort_keys=True) + "\n").encode("ascii")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise MutationProtocolRefused()
    return raw


def parse_response(raw: bytes) -> tuple[bool, str]:
    if type(raw) is not bytes or not raw or len(raw) > MAX_RESPONSE_BYTES:
        raise MutationProtocolRefused()
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_unique_pairs,
                           parse_constant=_reject_constant)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise MutationProtocolRefused() from None
    if (type(value) is not dict or set(value) != {"version", "ok", "code"}
        or type(value["version"]) is not int or value["version"] != VERSION
        or type(value["ok"]) is not bool or type(value["code"]) is not str
        or value["code"] not in RESULT_CODES
        or value["ok"] != (value["code"] in {"CHANGED", "NO_CHANGE"})):
        raise MutationProtocolRefused()
    return value["ok"], value["code"]
