"""Separate root entrypoint for strictly local ingress mutation."""

from __future__ import annotations

import os
import select
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from cloudflared_manager.activation.mutation_protocol import (
    MAX_REQUEST_BYTES, MutationProtocolRefused, MutationRequest, encode_response, parse_request,
)
from cloudflared_manager.activation.transaction import ActivationError, DeployedAuthority, FilesystemActivation
from cloudflared_manager.activation.filesystem import FilesystemRefused
from cloudflared_manager.cloudflared.editing import (
    ApplicationValidationError, CloudflaredValidationExecutionError,
    CloudflaredValidationRejectedError, CloudflaredValidationTimeoutError,
    CloudflaredValidatorUnavailableError, CandidateFileError, MutationRejectedError,
    RoundTripYamlError, SourceConfigChangedError, SourceConfigUnreadableError,
    StaleMutationError, UnsupportedConfigStructureError,
)
from cloudflared_manager.deployment.errors import UpdateLockedError
from cloudflared_manager.deployment.paths import DeploymentPaths

REQUEST_READ_TIMEOUT_SECONDS = 5.0


def _operation(request: MutationRequest):
    """Convert only the allowlisted domain request to an in-process callable."""
    if request.action == "local_ingress_add":
        assert request.route is not None
        return lambda document: document.add_local_hostname_ingress(request.route)
    if request.action == "local_ingress_edit":
        assert request.route is not None and request.target is not None
        return lambda document: document.edit_local_hostname_ingress(request.target, request.route)
    assert request.action == "local_ingress_delete" and request.target is not None
    return lambda document: document.delete_local_hostname_ingress(request.target)


def execute(request: MutationRequest, *, engine: FilesystemActivation | None = None,
            allowed_adopted_parent: Path = Path("/etc/cloudflared")) -> object:
    paths = DeploymentPaths()
    if engine is None:
        _, adopted = DeployedAuthority(paths).current()
        if adopted.parent != allowed_adopted_parent:
            raise MutationProtocolRefused("PRIVILEGED_BOUNDARY_UNAVAILABLE")
        engine = FilesystemActivation(paths)
    return engine.run(_operation(request),
                      expected_source_revision=request.source_revision,
                      allowed_adopted_parent=allowed_adopted_parent)


def dispatch(request: MutationRequest, operation: Callable[[MutationRequest], object]) -> tuple[bool, str]:
    try:
        result = operation(request)
        code = getattr(result, "code", None)
        if code == "COMMITTED_SUCCESS":
            return True, "CHANGED"
        if code == "NO_CHANGE":
            return True, "NO_CHANGE"
        if code == "FAILED_ROLLED_BACK":
            return False, "ACTIVATION_FAILED_ROLLED_BACK"
        if code in {"RECOVERY_REQUIRED", "CONFIG_RESTORED_SERVICE_RECOVERY_FAILED",
                    "ROLLBACK_FAILED_STATE_INDETERMINATE"}:
            return False, "RECOVERY_REQUIRED"
        return False, "ACTIVATION_FAILED"
    except MutationProtocolRefused as error:
        return False, error.code
    except StaleMutationError:
        return False, "STALE_CONFLICT"
    except SourceConfigChangedError:
        return False, "STALE_CONFLICT"
    except CandidateFileError:
        return False, "RECOVERY_REQUIRED"
    except SourceConfigUnreadableError:
        return False, "UNSUPPORTED_CONFIG"
    except MutationRejectedError:
        return False, "INVALID_DOMAIN_DATA"
    except (UnsupportedConfigStructureError, RoundTripYamlError):
        return False, "UNSUPPORTED_CONFIG"
    except (ApplicationValidationError, CloudflaredValidationExecutionError,
            CloudflaredValidationRejectedError, CloudflaredValidationTimeoutError,
            CloudflaredValidatorUnavailableError):
        return False, "VALIDATION_FAILED"
    except UpdateLockedError:
        return False, "BUSY"
    except FilesystemRefused as error:
        if error.code in {"NOT_ADOPTED", "STALE_RELEASE", "STALE_AUTHORITY"}:
            return False, "PRIVILEGED_BOUNDARY_UNAVAILABLE"
        if error.code in {"STALE_SOURCE", "SOURCE_CHANGED"}:
            return False, "STALE_CONFLICT"
        return False, "RECOVERY_REQUIRED"
    except ActivationError as error:
        if error.code == "FAILED_ROLLED_BACK":
            return False, "ACTIVATION_FAILED_ROLLED_BACK"
        if error.code in {"RECOVERY_REQUIRED", "CONFIG_RESTORED_SERVICE_RECOVERY_FAILED",
                          "ROLLBACK_FAILED_STATE_INDETERMINATE"}:
            return False, "RECOVERY_REQUIRED"
        if error.code == "PRIVILEGED_BOUNDARY_UNAVAILABLE":
            return False, "PRIVILEGED_BOUNDARY_UNAVAILABLE"
        if (error.code == "STALE_SOURCE"
            or (error.code == "FAILED_PRECOMMIT" and error.original == "STALE_SOURCE")):
            return False, "STALE_CONFLICT"
        return False, "ACTIVATION_FAILED"
    except Exception:
        return False, "PRIVILEGED_BOUNDARY_UNAVAILABLE"


def serve(stdin: BinaryIO, stdout: BinaryIO, *, operation: Callable[[MutationRequest], object] = execute,
          euid: Callable[[], int] = os.geteuid) -> int:
    try:
        if euid() != 0:
            raise MutationProtocolRefused("PRIVILEGED_BOUNDARY_UNAVAILABLE")
        request = parse_request(_read_request(stdin))
        ok, code = dispatch(request, operation)
    except MutationProtocolRefused as error:
        ok, code = False, error.code
    except Exception:
        ok, code = False, "PRIVILEGED_BOUNDARY_UNAVAILABLE"
    stdout.write(encode_response(ok=ok, code=code))
    stdout.flush()
    return 0 if ok else 1


def _read_request(stdin: BinaryIO) -> bytes:
    try:
        fd = stdin.fileno()
    except (AttributeError, OSError):
        return stdin.read(MAX_REQUEST_BYTES + 1)
    deadline = time.monotonic() + REQUEST_READ_TIMEOUT_SECONDS
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_REQUEST_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise MutationProtocolRefused()
        part = os.read(fd, MAX_REQUEST_BYTES + 1 - total)
        if not part:
            return b"".join(chunks)
        chunks.append(part)
        total += len(part)
    raise MutationProtocolRefused()


def main() -> int:
    os.umask(0o077)
    return serve(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    raise SystemExit(main())
