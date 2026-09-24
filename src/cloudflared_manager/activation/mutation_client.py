"""Unprivileged fixed-argv client for the separately installed mutation bridge."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from cloudflared_manager.activation.mutation_protocol import (
    MutationProtocolRefused, MutationRequest, encode_request, parse_response,
)

SUDO_PATH = Path("/usr/bin/sudo")
HELPER_PATH = Path("/opt/cloudflared-manager/privileged-mutation-helper")
MUTATION_TIMEOUT_SECONDS = 330


@dataclass(frozen=True, slots=True)
class MutationBridgeResult:
    ok: bool
    code: str


class MutationBridgeUnavailable(Exception):
    pass


def mutate(request: MutationRequest, *, runner=subprocess.run) -> MutationBridgeResult:
    try:
        encoded = encode_request(request)
        completed = runner(
            [str(SUDO_PATH), "-n", str(HELPER_PATH)],
            input=encoded, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=MUTATION_TIMEOUT_SECONDS, check=False, shell=False,
        )
        ok, code = parse_response(completed.stdout)
    except subprocess.TimeoutExpired:
        return MutationBridgeResult(False, "RECOVERY_REQUIRED")
    except (OSError, MutationProtocolRefused):
        raise MutationBridgeUnavailable("The privileged mutation helper is unavailable.") from None
    if completed.returncode != (0 if ok else 1):
        raise MutationBridgeUnavailable("The privileged mutation helper returned an invalid result.")
    return MutationBridgeResult(ok, code)
