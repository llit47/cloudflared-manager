"""Best-effort interpretation of local cloudflared runtime information."""

from __future__ import annotations

import os
import re
import shlex
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cloudflared_manager.cloudflared.errors import RuntimeDiscoveryError
from cloudflared_manager.cloudflared.models import CloudflaredRuntime, ManagementMode
from cloudflared_manager.cloudflared.runtime import (
    CommandResult,
    CommandRunner,
    DiscoveryCommand,
    SubprocessCommandRunner,
)

ExecutableFinder = Callable[[str], str | None]
ExecutableChecker = Callable[[Path], bool]
RuntimeDiscoveryProvider = Callable[[bool], CloudflaredRuntime | None]

_LOAD_STATES = {
    "bad-setting",
    "error",
    "generated",
    "loaded",
    "masked",
    "merged",
    "not-found",
    "stub",
    "transient",
}
_ACTIVE_STATES = {
    "active",
    "activating",
    "deactivating",
    "failed",
    "inactive",
    "maintenance",
    "refreshing",
    "reloading",
}
_ENABLED_STATES = {
    "alias",
    "disabled",
    "enabled",
    "enabled-runtime",
    "generated",
    "indirect",
    "linked",
    "linked-runtime",
    "masked",
    "masked-runtime",
    "static",
    "transient",
}
_SAFE_STATE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_VERSION = re.compile(
    r"\bcloudflared(?:\s+version)?\s+v?"
    r"([0-9]+(?:\.[0-9]+){1,3}(?:[-+][0-9A-Za-z.-]{1,32})?)"
    r"(?=\s|\(|$)",
    re.IGNORECASE,
)
_SYSTEMD_PATH = re.compile(r"(?:^|[{;]\s*)path=(/[^\s;}]+)")
_SYSTEMD_ARGV = re.compile(
    r"(?:^|;\s*)argv\[\]=(.*?)"
    r"(?=\s+;\s+(?:ignore_errors|start_time|stop_time|pid|code|status)=|\s*}\s*$)",
    re.DOTALL,
)
_SYSTEMD_HEX_ESCAPE = re.compile(r"\\x([0-9A-Fa-f]{2})")
_SAFE_ABSOLUTE_PATH = re.compile(r"^/[A-Za-z0-9._+:/-]{1,4094}$")
_MAX_PROPERTY_OUTPUT = 65_536


@dataclass(frozen=True, slots=True)
class _ExecStartFacts:
    executable_path: Path | None = None
    management_mode: ManagementMode = ManagementMode.UNKNOWN
    explicit_config_path: Path | None = None


@dataclass(frozen=True, slots=True)
class _SystemdFacts:
    load_state: str | None = None
    active_state: str | None = None
    sub_state: str | None = None
    main_pid: int | None = None
    exec_start: _ExecStartFacts = _ExecStartFacts()


def discover_cloudflared(
    enabled: bool,
    runner: CommandRunner | None = None,
    *,
    executable_finder: ExecutableFinder = shutil.which,
    executable_checker: ExecutableChecker | None = None,
) -> CloudflaredRuntime | None:
    """Discover sanitized local facts, or do nothing when explicitly disabled."""

    if not enabled:
        return None

    command_runner = runner or SubprocessCommandRunner()
    check_executable = executable_checker or _is_executable_file
    executable_path = _which_cloudflared(executable_finder)

    systemd_available = False
    service_exists: bool | None = None
    systemd_facts = _SystemdFacts()
    try:
        show_result = command_runner.run(DiscoveryCommand.SYSTEMD_SHOW)
    except RuntimeDiscoveryError:
        pass
    else:
        systemd_facts = _parse_systemd_properties(show_result)
        systemd_available = (
            show_result.returncode == 0 or systemd_facts.load_state is not None
        )
        service_exists = _service_exists(systemd_facts.load_state)

    if (
        executable_path is None
        and systemd_facts.exec_start.executable_path is not None
        and check_executable(systemd_facts.exec_start.executable_path)
    ):
        executable_path = systemd_facts.exec_start.executable_path

    version = _discover_version(command_runner, executable_path)
    enabled_state = _discover_enabled_state(command_runner, service_exists)

    return CloudflaredRuntime(
        executable_path=executable_path,
        version=version,
        systemd_available=systemd_available,
        service_exists=service_exists,
        load_state=systemd_facts.load_state,
        active_state=systemd_facts.active_state,
        sub_state=systemd_facts.sub_state,
        enabled_state=enabled_state,
        main_pid=systemd_facts.main_pid,
        management_mode=systemd_facts.exec_start.management_mode,
        explicit_config_path=systemd_facts.exec_start.explicit_config_path,
    )


def _which_cloudflared(executable_finder: ExecutableFinder) -> Path | None:
    found = executable_finder("cloudflared")
    if found is None:
        return None
    path = Path(found)
    if not path.is_absolute() or path.name != "cloudflared":
        return None
    return path


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _discover_version(
    runner: CommandRunner,
    executable_path: Path | None,
) -> str | None:
    if executable_path is None:
        return None
    try:
        result = runner.run(
            DiscoveryCommand.CLOUDFLARED_VERSION,
            cloudflared_executable=executable_path,
        )
    except RuntimeDiscoveryError:
        return None
    if result.returncode != 0:
        return None
    for output in (result.stdout, result.stderr):
        match = _VERSION.search(output[:_MAX_PROPERTY_OUTPUT])
        if match is not None:
            return match.group(1)
    return None


def _discover_enabled_state(
    runner: CommandRunner,
    service_exists: bool | None,
) -> str | None:
    if service_exists is not True:
        return None
    try:
        result = runner.run(DiscoveryCommand.SYSTEMD_IS_ENABLED)
    except RuntimeDiscoveryError:
        return None

    state = result.stdout.strip().splitlines()[0].lower() if result.stdout.strip() else ""
    return state if state in _ENABLED_STATES else None


def _parse_systemd_properties(result: CommandResult) -> _SystemdFacts:
    load_state = None
    active_state = None
    sub_state = None
    main_pid = None
    exec_start = _ExecStartFacts()

    for line in result.stdout[:_MAX_PROPERTY_OUTPUT].splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        if key == "LoadState":
            load_state = _known_state(value, _LOAD_STATES)
        elif key == "ActiveState":
            active_state = _known_state(value, _ACTIVE_STATES)
        elif key == "SubState":
            sub_state = _sanitized_state(value)
        elif key == "MainPID":
            main_pid = _positive_integer(value)
        elif key == "ExecStart":
            exec_start = _parse_exec_start(value)

    return _SystemdFacts(
        load_state=load_state,
        active_state=active_state,
        sub_state=sub_state,
        main_pid=main_pid,
        exec_start=exec_start,
    )


def _parse_exec_start(raw_value: str) -> _ExecStartFacts:
    raw_value = raw_value[:_MAX_PROPERTY_OUTPUT]
    path_matches = list(_SYSTEMD_PATH.finditer(raw_value))
    argv_matches = list(_SYSTEMD_ARGV.finditer(raw_value))
    if len(path_matches) > 1 or len(argv_matches) > 1:
        return _ExecStartFacts()

    executable_path = _safe_cloudflared_path(
        path_matches[0].group(1) if path_matches else None
    )
    if not argv_matches:
        return _ExecStartFacts(executable_path=executable_path)

    decoded_argv = _SYSTEMD_HEX_ESCAPE.sub(
        lambda match: chr(int(match.group(1), 16)),
        argv_matches[0].group(1),
    )
    try:
        arguments = shlex.split(decoded_argv, posix=True)
    except ValueError:
        return _ExecStartFacts(executable_path=executable_path)

    if executable_path is None and arguments:
        executable_path = _safe_cloudflared_path(arguments[0])

    has_token = any(
        argument in {"--token", "--token-file"}
        or argument.startswith(("--token=", "--token-file="))
        for argument in arguments
    )
    config_value = _flag_value(arguments, "--config")
    explicit_config_path = _safe_absolute_path(config_value)

    if has_token:
        management_mode = ManagementMode.REMOTE_TOKEN
    elif config_value is not None:
        management_mode = ManagementMode.LOCAL_CONFIG
    else:
        management_mode = ManagementMode.UNKNOWN

    return _ExecStartFacts(
        executable_path=executable_path,
        management_mode=management_mode,
        explicit_config_path=explicit_config_path,
    )


def _flag_value(arguments: list[str], flag: str) -> str | None:
    prefix = f"{flag}="
    for position, argument in enumerate(arguments):
        if argument.startswith(prefix):
            value = argument.removeprefix(prefix)
            return value or None
        if argument == flag and position + 1 < len(arguments):
            value = arguments[position + 1]
            return value if value and not value.startswith("--") else None
    return None


def _safe_cloudflared_path(value: str | None) -> Path | None:
    path = _safe_absolute_path(value)
    return path if path is not None and path.name == "cloudflared" else None


def _safe_absolute_path(value: str | None) -> Path | None:
    if (
        value is None
        or _SAFE_ABSOLUTE_PATH.fullmatch(value) is None
        or value.startswith("//")
    ):
        return None
    path = Path(value)
    return (
        path
        if path.is_absolute() and str(path) == value and ".." not in path.parts
        else None
    )


def _known_state(value: str, known: set[str]) -> str | None:
    normalized = value.strip().lower()
    return normalized if normalized in known else None


def _sanitized_state(value: str) -> str | None:
    normalized = value.strip().lower()
    return normalized if _SAFE_STATE.fullmatch(normalized) else None


def _positive_integer(value: str) -> int | None:
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _service_exists(load_state: str | None) -> bool | None:
    if load_state is None:
        return None
    return load_state != "not-found"
