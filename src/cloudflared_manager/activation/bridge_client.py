"""Unprivileged fixed-argv sudo client; no web route invokes it yet."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cloudflared_manager.activation.bridge_protocol import (
    MAX_RESPONSE_BYTES, PROTOCOL_VERSION,
)

HELPER_PATH = Path("/opt/cloudflared-manager/privileged-helper")
SUDO_PATH = Path("/usr/bin/sudo")
_RESULT_CODES = frozenset({
    "NO_RECOVERY_REQUIRED", "COMMITTED_SUCCESS", "FAILED_PRECOMMIT",
    "FAILED_ROLLED_BACK", "RECOVERY_REQUIRED",
    "CONFIG_RESTORED_SERVICE_RECOVERY_FAILED", "ROLLBACK_FAILED_STATE_INDETERMINATE",
    "INVALID_REQUEST", "UNSUPPORTED_VERSION", "UNKNOWN_OPERATION",
    "PRIVILEGED_BOUNDARY_UNAVAILABLE", "UNSUPPORTED_ADOPTED_PATH", "ACTIVATION_FAILED",
})


@dataclass(frozen=True, slots=True)
class BridgeResult:
    ok: bool
    code: str


class BridgeUnavailable(Exception):
    pass


def recover(*, runner=subprocess.run) -> BridgeResult:
    request = b'{"version":1,"operation":"recover"}\n'
    try:
        completed = runner(
            [str(SUDO_PATH), "-n", str(HELPER_PATH)],
            input=request, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120, check=False, shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BridgeUnavailable("The privileged helper is unavailable.") from None
    if len(completed.stdout) > MAX_RESPONSE_BYTES or completed.returncode not in (0, 1):
        raise BridgeUnavailable("The privileged helper returned an invalid result.")
    try:
        response = json.loads(completed.stdout)
    except (UnicodeError, ValueError, TypeError):
        raise BridgeUnavailable("The privileged helper returned an invalid result.") from None
    if (type(response) is not dict or set(response) != {"version", "ok", "code"}
        or type(response["version"]) is not int or response["version"] != PROTOCOL_VERSION
        or type(response["ok"]) is not bool or type(response["code"]) is not str
        or response["code"] not in _RESULT_CODES
        or completed.returncode != (0 if response["ok"] else 1)):
        raise BridgeUnavailable("The privileged helper returned an invalid result.")
    return BridgeResult(response["ok"], response["code"])
