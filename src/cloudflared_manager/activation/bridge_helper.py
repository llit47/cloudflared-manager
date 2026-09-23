"""Single root entrypoint for the versioned activation bridge."""

from __future__ import annotations

import os
import select
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from cloudflared_manager.activation.bridge_protocol import (
    MAX_REQUEST_BYTES, ProtocolRefused, Request, encode_response, parse_request,
)
from cloudflared_manager.activation.transaction import (
    ActivationError, DeployedAuthority, FilesystemActivation,
)
from cloudflared_manager.deployment.paths import DeploymentPaths

_SUCCESS_CODES = frozenset({
    "NO_RECOVERY_REQUIRED", "COMMITTED_SUCCESS", "FAILED_PRECOMMIT",
    "FAILED_ROLLED_BACK", "RECOVERY_REQUIRED",
    "CONFIG_RESTORED_SERVICE_RECOVERY_FAILED", "ROLLBACK_FAILED_STATE_INDETERMINATE",
})
REQUEST_READ_TIMEOUT_SECONDS = 5.0


def dispatch(request: Request, recover: Callable[[], object]) -> tuple[bool, str]:
    # The parser has exactly one operation. Never dispatch from caller text.
    if request.operation != "recover":
        return False, "UNKNOWN_OPERATION"
    try:
        result = recover()
    except ProtocolRefused as error:
        return False, error.code
    except ActivationError as error:
        return False, error.code if error.code in _SUCCESS_CODES else "ACTIVATION_FAILED"
    except Exception:
        return False, "ACTIVATION_FAILED"
    code = getattr(result, "code", None)
    if code not in _SUCCESS_CODES:
        return False, "ACTIVATION_FAILED"
    return code in {"NO_RECOVERY_REQUIRED", "COMMITTED_SUCCESS"}, code


def serve(stdin: BinaryIO, stdout: BinaryIO, *, recover: Callable[[], object],
          euid: Callable[[], int] = os.geteuid) -> int:
    try:
        if euid() != 0:
            raise ProtocolRefused("PRIVILEGED_BOUNDARY_UNAVAILABLE")
        raw = _read_request(stdin)
        request = parse_request(raw)
        ok, code = dispatch(request, recover)
    except ProtocolRefused as error:
        ok, code = False, error.code
    except Exception:
        ok, code = False, "ACTIVATION_FAILED"
    stdout.write(encode_response(ok=ok, code=code))
    stdout.flush()
    return 0 if ok else 1


def _read_request(stdin: BinaryIO) -> bytes:
    """Bound both bytes and time spent holding one privileged helper process."""
    try:
        fd = stdin.fileno()
    except (AttributeError, OSError):
        return stdin.read(MAX_REQUEST_BYTES + 1)
    deadline = time.monotonic() + REQUEST_READ_TIMEOUT_SECONDS
    chunks: list[bytes] = []
    size = 0
    while size <= MAX_REQUEST_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise ProtocolRefused("INVALID_REQUEST")
        chunk = os.read(fd, MAX_REQUEST_BYTES + 1 - size)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
    raise ProtocolRefused("INVALID_REQUEST")


def _production_recovery() -> object:
    paths = DeploymentPaths()
    _, adopted = DeployedAuthority(paths).current()
    if adopted.parent != Path("/etc/cloudflared"):
        raise ProtocolRefused("UNSUPPORTED_ADOPTED_PATH")
    return FilesystemActivation(paths).recover()


def main() -> int:
    os.umask(0o077)
    return serve(sys.stdin.buffer, sys.stdout.buffer, recover=_production_recovery)


if __name__ == "__main__":
    raise SystemExit(main())
