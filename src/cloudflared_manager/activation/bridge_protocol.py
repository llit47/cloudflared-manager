"""The deliberately small, hostile-input protocol for the root helper."""

from __future__ import annotations

import json
from dataclasses import dataclass

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 4096


class ProtocolRefused(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Request:
    operation: str


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolRefused("INVALID_REQUEST")
        result[key] = value
    return result


def parse_request(raw: bytes) -> Request:
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise ProtocolRefused("INVALID_REQUEST")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeError, ValueError, TypeError, RecursionError) as error:
        raise ProtocolRefused("INVALID_REQUEST") from None
    if type(value) is not dict:
        raise ProtocolRefused("INVALID_REQUEST")
    if type(value.get("version")) is not int or value["version"] != PROTOCOL_VERSION:
        raise ProtocolRefused("UNSUPPORTED_VERSION")
    if type(value.get("operation")) is not str or value["operation"] != "recover":
        raise ProtocolRefused("UNKNOWN_OPERATION")
    if set(value) != {"version", "operation"}:
        raise ProtocolRefused("INVALID_REQUEST")
    return Request(operation="recover")


def encode_response(*, ok: bool, code: str) -> bytes:
    return (json.dumps({"version": PROTOCOL_VERSION, "ok": ok, "code": code},
                       separators=(",", ":"), sort_keys=True) + "\n").encode("ascii")
