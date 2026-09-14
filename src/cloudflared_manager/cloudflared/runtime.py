"""Controlled execution of allowlisted local discovery commands."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Protocol

from cloudflared_manager.cloudflared.errors import (
    CommandExecutionError,
    CommandTimedOutError,
    CommandUnavailableError,
)

DEFAULT_COMMAND_TIMEOUT_SECONDS = 3.0
SYSTEMD_UNIT = "cloudflared.service"


class DiscoveryCommand(Enum):
    """The complete allowlist of commands available to runtime discovery."""

    CLOUDFLARED_VERSION = auto()
    SYSTEMD_SHOW = auto()
    SYSTEMD_IS_ENABLED = auto()


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured output from one completed discovery command."""

    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Replaceable executor for the fixed discovery command allowlist."""

    def run(
        self,
        command: DiscoveryCommand,
        *,
        cloudflared_executable: Path | None = None,
    ) -> CommandResult:
        """Execute one allowlisted command with controlled arguments."""


@dataclass(frozen=True, slots=True)
class SubprocessCommandRunner:
    """Run fixed local inspection commands without a shell."""

    timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS

    def run(
        self,
        command: DiscoveryCommand,
        *,
        cloudflared_executable: Path | None = None,
    ) -> CommandResult:
        argv = self._arguments(command, cloudflared_executable)

        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                shell=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            failure = CommandTimedOutError(
                "A local runtime discovery command timed out."
            )
        except (OSError, UnicodeError):
            failure = CommandExecutionError(
                "A local runtime discovery command could not be executed."
            )
        else:
            return CommandResult(
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )

        raise failure

    @staticmethod
    def _arguments(
        command: DiscoveryCommand,
        cloudflared_executable: Path | None,
    ) -> list[str]:
        if command is DiscoveryCommand.CLOUDFLARED_VERSION:
            if (
                cloudflared_executable is None
                or not cloudflared_executable.is_absolute()
                or cloudflared_executable.name != "cloudflared"
            ):
                raise CommandUnavailableError(
                    "The cloudflared executable is unavailable for version inspection."
                )
            return [str(cloudflared_executable), "--version"]

        systemctl = shutil.which("systemctl")
        if (
            systemctl is None
            or not Path(systemctl).is_absolute()
            or Path(systemctl).name != "systemctl"
        ):
            raise CommandUnavailableError(
                "The systemd inspection command is unavailable."
            )

        if command is DiscoveryCommand.SYSTEMD_SHOW:
            return [
                systemctl,
                "show",
                SYSTEMD_UNIT,
                "--no-pager",
                "--property=LoadState,ActiveState,SubState,MainPID,ExecStart",
            ]
        if command is DiscoveryCommand.SYSTEMD_IS_ENABLED:
            return [systemctl, "is-enabled", SYSTEMD_UNIT]

        raise CommandUnavailableError(
            "The requested runtime discovery command is not allowlisted."
        )
