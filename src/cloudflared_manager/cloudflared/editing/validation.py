"""Layered validation interfaces for staged cloudflared candidates."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cloudflared_manager.cloudflared.editing.candidate import CandidateFile
from cloudflared_manager.cloudflared.editing.errors import (
    CloudflaredValidationExecutionError,
    CloudflaredValidationRejectedError,
    CloudflaredValidationTimeoutError,
    CloudflaredValidatorUnavailableError,
)

DEFAULT_VALIDATION_TIMEOUT_SECONDS = 5.0
MAX_VALIDATION_TIMEOUT_SECONDS = 30.0
_MAX_CAPTURED_OUTPUT_BYTES = 65_536


@dataclass(frozen=True, slots=True, repr=False)
class ValidationCommandResult:
    """Internal process result whose potentially sensitive output is never exposed."""

    returncode: int
    stdout: bytes
    stderr: bytes

    def __repr__(self) -> str:
        return f"ValidationCommandResult(returncode={self.returncode})"


class ValidationCommandRunner(Protocol):
    """Replaceable executor for one explicit cloudflared validation argv."""

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        pass_fds: tuple[int, ...],
        executable_fd: int | None = None,
    ) -> ValidationCommandResult:
        """Execute a command without a shell and return sanitized process state."""


@dataclass(frozen=True, slots=True)
class SubprocessValidationCommandRunner:
    """Execute a fixed argv directly, capturing only bounded retained output."""

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        pass_fds: tuple[int, ...],
        executable_fd: int | None = None,
    ) -> ValidationCommandResult:
        _require_single_directory_fd(pass_fds)
        if executable_fd is not None:
            try:
                info = os.fstat(executable_fd)
            except OSError as error:
                raise CloudflaredValidationExecutionError(
                    "The verified cloudflared executable is unavailable."
                ) from error
            if not stat.S_ISREG(info.st_mode) or executable_fd == pass_fds[0]:
                raise CloudflaredValidationExecutionError(
                    "The verified cloudflared executable is unsafe."
                )
        try:
            completed = subprocess.run(
                list(argv),
                executable=f"/proc/self/fd/{executable_fd}" if executable_fd is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                close_fds=True,
                pass_fds=pass_fds + ((executable_fd,) if executable_fd is not None else ()),
                shell=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise CloudflaredValidationTimeoutError(
                "Cloudflared candidate validation timed out."
            ) from error
        except (OSError, ValueError) as error:
            raise CloudflaredValidationExecutionError(
                "Cloudflared candidate validation could not be executed."
            ) from error
        return ValidationCommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout[:_MAX_CAPTURED_OUTPUT_BYTES],
            stderr=completed.stderr[:_MAX_CAPTURED_OUTPUT_BYTES],
        )


@dataclass(frozen=True, slots=True)
class CloudflaredValidationReport:
    """Sanitized proof that cloudflared accepted a staged candidate."""

    accepted: bool = True


class CandidateValidator(Protocol):
    def validate(self, candidate: CandidateFile) -> CloudflaredValidationReport:
        """Validate the internally generated candidate path."""


class CloudflaredCandidateValidator:
    """Validate candidates with the installed binary and exact supported argv."""

    def __init__(
        self,
        *,
        runner: ValidationCommandRunner | None = None,
        executable_finder: Callable[[str], str | None] = shutil.which,
        executable: Path | None = None,
        executable_fd: int | None = None,
        timeout_seconds: float = DEFAULT_VALIDATION_TIMEOUT_SECONDS,
    ) -> None:
        if (
            timeout_seconds <= 0
            or timeout_seconds > MAX_VALIDATION_TIMEOUT_SECONDS
        ):
            raise ValueError("cloudflared validation timeout is outside safe bounds")
        self._runner = runner or SubprocessValidationCommandRunner()
        self._executable_finder = executable_finder
        if (executable is None) != (executable_fd is None):
            raise ValueError("A verified executable path and descriptor are required together")
        if executable is not None and (not executable.is_absolute() or executable.name != "cloudflared"):
            raise ValueError("Invalid verified cloudflared executable")
        self._executable = executable
        self._executable_fd = executable_fd
        self._timeout_seconds = timeout_seconds

    def validate(self, candidate: CandidateFile) -> CloudflaredValidationReport:
        candidate.require_intact()
        found = str(self._executable) if self._executable is not None else self._executable_finder("cloudflared")
        if found is None:
            raise CloudflaredValidatorUnavailableError(
                "The cloudflared executable is unavailable for candidate validation."
            )
        executable = Path(found)
        if not executable.is_absolute() or executable.name != "cloudflared":
            raise CloudflaredValidatorUnavailableError(
                "The cloudflared executable is unavailable for candidate validation."
            )

        binding = candidate.validation_binding()
        argv = (
            str(executable),
            "tunnel",
            "--config",
            str(binding.path),
            "ingress",
            "validate",
        )
        kwargs = {"timeout_seconds": self._timeout_seconds, "pass_fds": binding.pass_fds}
        if self._executable_fd is not None:
            kwargs["executable_fd"] = self._executable_fd
        result = self._runner.run(argv, **kwargs)
        candidate.require_intact()
        if result.returncode != 0:
            raise CloudflaredValidationRejectedError(
                "Cloudflared rejected the candidate configuration."
            )
        return CloudflaredValidationReport()


def _require_single_directory_fd(pass_fds: tuple[int, ...]) -> None:
    if (
        len(pass_fds) != 1
        or isinstance(pass_fds[0], bool)
        or not isinstance(pass_fds[0], int)
        or pass_fds[0] < 0
    ):
        raise CloudflaredValidationExecutionError(
            "Cloudflared validation did not receive one controlled directory descriptor."
        )
    try:
        metadata = os.fstat(pass_fds[0])
        inheritable = os.get_inheritable(pass_fds[0])
    except OSError as error:
        raise CloudflaredValidationExecutionError(
            "Cloudflared validation directory identity is unavailable."
        ) from error
    if not stat.S_ISDIR(metadata.st_mode) or inheritable:
        raise CloudflaredValidationExecutionError(
            "Cloudflared validation directory identity is unsafe."
        )
