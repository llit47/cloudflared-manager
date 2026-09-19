"""Safe parsing and atomic updates for the systemd EnvironmentFile."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cloudflared_manager.deployment.errors import EnvironmentFileError

MAX_ENVIRONMENT_BYTES = 65_536
MANAGED_KEY_ORDER = (
    "CFM_APP_NAME",
    "CFM_MODE",
    "CFM_BIND_HOST",
    "CFM_BIND_PORT",
    "CFM_RUNTIME_DISCOVERY_ENABLED",
)
MANAGED_KEYS = frozenset(MANAGED_KEY_ORDER)
_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:-]+$")


@dataclass(frozen=True, slots=True)
class EnvironmentDocument:
    """EnvironmentFile text with loss-minimizing managed-key updates."""

    lines: tuple[str, ...]

    @classmethod
    def parse(cls, text: str) -> EnvironmentDocument:
        if "\x00" in text:
            raise EnvironmentFileError("The manager environment file contains NUL data.")
        return cls(tuple(text.splitlines()))

    def managed_values(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in self.lines:
            match = _ASSIGNMENT.fullmatch(line)
            if match is None:
                continue
            key, value = match.groups()
            if key not in MANAGED_KEYS:
                continue
            if key in values:
                raise EnvironmentFileError(
                    f"The manager environment file contains duplicate {key} entries."
                )
            if _SAFE_VALUE.fullmatch(value) is None:
                raise EnvironmentFileError(
                    f"The managed value for {key} is not in the supported safe format."
                )
            values[key] = value
        return values

    def updated(self, updates: dict[str, str]) -> EnvironmentDocument:
        _validate_updates(updates)
        self.managed_values()
        remaining = dict(updates)
        rendered: list[str] = []
        for line in self.lines:
            match = _ASSIGNMENT.fullmatch(line)
            if match is not None and match.group(1) in remaining:
                key = match.group(1)
                rendered.append(f"{key}={remaining.pop(key)}")
            else:
                rendered.append(line)
        if remaining:
            if rendered and rendered[-1] != "":
                rendered.append("")
            for key in MANAGED_KEY_ORDER:
                if key in remaining:
                    rendered.append(f"{key}={remaining[key]}")
        document = EnvironmentDocument(tuple(rendered))
        document.managed_values()
        return document

    def render(self) -> str:
        return "\n".join(self.lines).rstrip("\n") + "\n"


def initial_environment(bind_host: str, bind_port: int) -> EnvironmentDocument:
    """Create the only production capability supported by this release."""

    return EnvironmentDocument(
        (
            "# Managed with cfm-config. Unknown keys and comments are preserved.",
            "CFM_APP_NAME=cloudflared-manager",
            "CFM_MODE=production",
            f"CFM_BIND_HOST={bind_host}",
            f"CFM_BIND_PORT={bind_port}",
            "CFM_RUNTIME_DISCOVERY_ENABLED=true",
        )
    )


def require_safe_environment(path: Path, *, owner: tuple[int, int] | None) -> None:
    """Validate owned configuration before reading it or changing any metadata."""

    try:
        parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_mode & 0o022
            or (owner is not None and (parent.st_uid, parent.st_gid) != owner)
        ):
            raise EnvironmentFileError("The manager configuration directory is unsafe.")
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (owner is not None and (metadata.st_uid, metadata.st_gid) != owner)
        ):
            raise EnvironmentFileError("The manager environment file has unsafe ownership or permissions.")
    except OSError as error:
        raise EnvironmentFileError("The manager environment file cannot be inspected safely.") from error


def read_environment(path: Path) -> tuple[EnvironmentDocument, bytes]:
    """Read a small regular file without following a final symlink."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as error:
        raise EnvironmentFileError("The manager environment file cannot be inspected.") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise EnvironmentFileError("The manager environment path is not a regular file.")
    if metadata.st_size > MAX_ENVIRONMENT_BYTES:
        raise EnvironmentFileError("The manager environment file is unexpectedly large.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(MAX_ENVIRONMENT_BYTES + 1)
    except OSError as error:
        raise EnvironmentFileError("The manager environment file cannot be read.") from error
    if len(raw) > MAX_ENVIRONMENT_BYTES:
        raise EnvironmentFileError("The manager environment file is unexpectedly large.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise EnvironmentFileError("The manager environment file is not UTF-8 text.") from error
    return EnvironmentDocument.parse(text), raw


def atomic_write_environment(
    path: Path,
    content: EnvironmentDocument | bytes,
    *,
    owner: tuple[int, int] | None = (0, 0),
) -> None:
    """Replace the fixed environment file atomically with mode 0600."""

    _validate_parent_directory(path.parent)
    if path.is_symlink():
        raise EnvironmentFileError("Refusing to replace a symlinked environment file.")
    raw = content.render().encode() if isinstance(content, EnvironmentDocument) else content
    if len(raw) > MAX_ENVIRONMENT_BYTES or b"\x00" in raw:
        raise EnvironmentFileError("The manager environment content is invalid.")

    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".cloudflared-manager.env.",
            dir=path.parent,
        )
        os.fchmod(descriptor, 0o600)
        if owner is not None:
            os.fchown(descriptor, owner[0], owner[1])
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as error:
        raise EnvironmentFileError("The manager environment file could not be written safely.") from error
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _validate_updates(updates: dict[str, str]) -> None:
    for key, value in updates.items():
        if key not in MANAGED_KEYS:
            raise EnvironmentFileError(f"{key} is not managed by this release.")
        if _SAFE_VALUE.fullmatch(value) is None:
            raise EnvironmentFileError(f"The value for {key} is not safe for EnvironmentFile.")


def _validate_parent_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise EnvironmentFileError("The manager configuration directory is unavailable.") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise EnvironmentFileError("The manager configuration directory is unsafe.")
