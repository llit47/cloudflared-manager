"""Fixed manager-only systemd operations and read-only network inspection."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cloudflared_manager.deployment.errors import HostOperationError
from cloudflared_manager.deployment.network import NetworkInspector
from cloudflared_manager.deployment.validation import validate_interface

MANAGER_UNIT = "cloudflared-manager.service"
_SAFE_STATE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


@dataclass(frozen=True, slots=True)
class CommandOutput:
    returncode: int
    stdout: str = ""


class SystemdManager:
    """Expose only the systemd operations permitted for the manager unit."""

    def __init__(self, executable: str | None = None, timeout: float = 20.0) -> None:
        resolved = executable or shutil.which("systemctl")
        if (
            resolved is None
            or not Path(resolved).is_absolute()
            or Path(resolved).name != "systemctl"
        ):
            raise HostOperationError("systemctl is unavailable.")
        self._executable = str(Path(resolved))
        self._timeout = timeout

    def daemon_reload(self) -> None:
        self._require_success(("daemon-reload",))

    def enable(self) -> None:
        self._require_success(("enable", MANAGER_UNIT))

    def disable(self) -> None:
        self._require_success(("disable", MANAGER_UNIT))

    def start(self) -> None:
        self._require_success(("start", MANAGER_UNIT))

    def stop(self) -> None:
        self._require_success(("stop", MANAGER_UNIT))

    def restart(self) -> None:
        self._require_success(("restart", MANAGER_UNIT))

    def is_active(self) -> bool:
        result = self._run(("is-active", "--quiet", MANAGER_UNIT))
        return result.returncode == 0

    def is_enabled(self) -> bool:
        result = self._run(("is-enabled", "--quiet", MANAGER_UNIT))
        return result.returncode == 0

    def sanitized_status(self) -> tuple[str | None, str | None, str | None]:
        result = self._run(
            (
                "show",
                MANAGER_UNIT,
                "--no-pager",
                "--property=LoadState,ActiveState,SubState",
            )
        )
        states: dict[str, str] = {}
        for line in result.stdout[:4096].splitlines():
            key, separator, value = line.partition("=")
            normalized = value.strip().lower()
            if separator and key in {"LoadState", "ActiveState", "SubState"}:
                if _SAFE_STATE.fullmatch(normalized):
                    states[key] = normalized
        return states.get("LoadState"), states.get("ActiveState"), states.get("SubState")

    def _require_success(self, arguments: tuple[str, ...]) -> None:
        if self._run(arguments).returncode != 0:
            raise HostOperationError("A cloudflared-manager systemd operation failed.")

    def _run(self, arguments: tuple[str, ...]) -> CommandOutput:
        try:
            result = subprocess.run(
                [self._executable, *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HostOperationError("A cloudflared-manager systemd operation failed.") from error
        return CommandOutput(returncode=result.returncode, stdout=result.stdout)


class IpNetworkInspector(NetworkInspector):
    """Run the two fixed read-only `ip` observations used for LAN selection."""

    def __init__(self, executable: str | None = None, timeout: float = 5.0) -> None:
        resolved = executable or shutil.which("ip")
        if resolved is None or not Path(resolved).is_absolute() or Path(resolved).name != "ip":
            raise HostOperationError("The ip command is required for LAN address selection.")
        self._executable = str(Path(resolved))
        self._timeout = timeout

    def default_routes(self) -> str:
        return self._run([self._executable, "-4", "route", "show", "default"])

    def global_addresses(self, interface: str) -> str:
        validated = validate_interface(interface)
        return self._run(
            [self._executable, "-4", "-o", "addr", "show", "dev", validated, "scope", "global"]
        )

    def all_global_addresses(self) -> str:
        return self._run(
            [self._executable, "-4", "-o", "addr", "show", "scope", "global"]
        )

    def _run(self, arguments: list[str]) -> str:
        try:
            result = subprocess.run(
                arguments,
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HostOperationError("LAN address inspection failed.") from error
        if result.returncode != 0:
            raise HostOperationError("LAN address inspection failed.")
        return result.stdout
