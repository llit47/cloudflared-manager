"""Immutable, bounded snapshots of an adopted cloudflared config source."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from cloudflared_manager.cloudflared.editing.errors import (
    SourceConfigChangedError,
    SourceConfigUnreadableError,
)
from cloudflared_manager.cloudflared.limits import MAX_CLOUDFLARED_CONFIG_BYTES

_READ_CHUNK_BYTES = 65_536


@dataclass(frozen=True, slots=True, repr=False)
class ConfigSourceSnapshot:
    """Source bytes and identity facts needed for later stale-write checks."""

    path: Path
    original_bytes: bytes
    sha256: str
    size: int
    device: int
    inode: int
    permission_mode: int
    uid: int
    gid: int
    mtime_ns: int
    ctime_ns: int
    parent_device: int
    parent_inode: int
    parent_mode: int
    parent_uid: int
    parent_gid: int

    def __repr__(self) -> str:
        return (
            "ConfigSourceSnapshot("
            f"sha256={self.sha256!r}, size={self.size}, "
            f"device={self.device}, inode={self.inode})"
        )


def read_config_source_snapshot(path: str | Path) -> ConfigSourceSnapshot:
    """Read a canonical regular file without following symlinks or exceeding limits."""

    source = Path(path)
    parent_descriptor: int | None = None
    descriptor: int | None = None
    try:
        if not source.is_absolute() or source.resolve(strict=True) != source:
            raise SourceConfigUnreadableError(
                "The source configuration path is not a safe canonical path."
            )

        parent_metadata = source.parent.lstat()
        path_metadata = source.lstat()
        if not stat.S_ISDIR(parent_metadata.st_mode):
            raise SourceConfigUnreadableError(
                "The source configuration directory is not safe."
            )
        _require_regular_bounded(path_metadata)

        no_follow = getattr(os, "O_NOFOLLOW", None)
        directory_flag = getattr(os, "O_DIRECTORY", None)
        if no_follow is None or directory_flag is None:
            raise SourceConfigUnreadableError(
                "Safe directory-relative file access is unavailable on this platform."
            )
        close_on_exec = getattr(os, "O_CLOEXEC", 0)
        parent_descriptor = os.open(
            source.parent,
            os.O_RDONLY | directory_flag | no_follow | close_on_exec,
        )
        opened_parent_metadata = os.fstat(parent_descriptor)
        if not _same_directory(parent_metadata, opened_parent_metadata):
            raise SourceConfigChangedError(
                "The source configuration directory changed while it was opened."
            )

        flags = os.O_RDONLY | no_follow | close_on_exec
        descriptor = os.open(source.name, flags, dir_fd=parent_descriptor)
        opened_metadata = os.fstat(descriptor)
        _require_regular_bounded(opened_metadata)
        if not _same_file(path_metadata, opened_metadata):
            raise SourceConfigChangedError(
                "The source configuration changed while it was being opened."
            )

        contents = _read_bounded(descriptor)
        final_metadata = os.fstat(descriptor)
        final_path_metadata = os.stat(
            source.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        final_parent_metadata = os.fstat(parent_descriptor)
        final_parent_path_metadata = source.parent.lstat()
        if (
            not _same_snapshot(opened_metadata, final_metadata)
            or not _same_snapshot(final_metadata, final_path_metadata)
            or not _same_directory(opened_parent_metadata, final_parent_metadata)
            or not _same_directory(
                final_parent_metadata,
                final_parent_path_metadata,
            )
            or len(contents) != final_metadata.st_size
        ):
            raise SourceConfigChangedError(
                "The source configuration changed while it was being read."
            )
    except (SourceConfigUnreadableError, SourceConfigChangedError):
        raise
    except (OSError, RuntimeError) as error:
        raise SourceConfigUnreadableError(
            "The source configuration could not be read safely."
        ) from error
    finally:
        close_error: OSError | None = None
        for opened_descriptor in (descriptor, parent_descriptor):
            if opened_descriptor is None:
                continue
            try:
                os.close(opened_descriptor)
            except OSError as error:
                close_error = close_error or error
        if close_error is not None:
            raise SourceConfigUnreadableError(
                "The source configuration could not be closed safely."
            ) from close_error

    return ConfigSourceSnapshot(
        path=source,
        original_bytes=contents,
        sha256=hashlib.sha256(contents).hexdigest(),
        size=len(contents),
        device=final_metadata.st_dev,
        inode=final_metadata.st_ino,
        permission_mode=stat.S_IMODE(final_metadata.st_mode),
        uid=final_metadata.st_uid,
        gid=final_metadata.st_gid,
        mtime_ns=final_metadata.st_mtime_ns,
        ctime_ns=final_metadata.st_ctime_ns,
        parent_device=final_parent_metadata.st_dev,
        parent_inode=final_parent_metadata.st_ino,
        parent_mode=stat.S_IMODE(final_parent_metadata.st_mode),
        parent_uid=final_parent_metadata.st_uid,
        parent_gid=final_parent_metadata.st_gid,
    )


def require_source_unchanged(snapshot: ConfigSourceSnapshot) -> None:
    """Fail if the path, bytes, metadata, or parent identity no longer match."""

    try:
        current = read_config_source_snapshot(snapshot.path)
    except (SourceConfigUnreadableError, SourceConfigChangedError) as error:
        raise SourceConfigChangedError(
            "The source configuration is no longer the snapshotted file."
        ) from error

    comparable = (
        "sha256",
        "size",
        "device",
        "inode",
        "permission_mode",
        "uid",
        "gid",
        "mtime_ns",
        "ctime_ns",
        "parent_device",
        "parent_inode",
        "parent_mode",
        "parent_uid",
        "parent_gid",
    )
    if any(getattr(current, field) != getattr(snapshot, field) for field in comparable):
        raise SourceConfigChangedError(
            "The source configuration changed while a candidate was prepared."
        )


def _read_bounded(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(
            descriptor,
            min(_READ_CHUNK_BYTES, MAX_CLOUDFLARED_CONFIG_BYTES + 1 - total),
        )
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_CLOUDFLARED_CONFIG_BYTES:
            raise SourceConfigUnreadableError(
                "The source configuration exceeds the supported size limit."
            )


def _require_regular_bounded(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise SourceConfigUnreadableError(
            "The source configuration is not a regular file."
        )
    if metadata.st_size < 0 or metadata.st_size > MAX_CLOUDFLARED_CONFIG_BYTES:
        raise SourceConfigUnreadableError(
            "The source configuration exceeds the supported size limit."
        )


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _same_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_uid,
        left.st_gid,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_uid,
        right.st_gid,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _same_directory(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(left.st_mode)
        and stat.S_ISDIR(right.st_mode)
        and (
            left.st_dev,
            left.st_ino,
            left.st_mode,
            left.st_uid,
            left.st_gid,
        )
        == (
            right.st_dev,
            right.st_ino,
            right.st_mode,
            right.st_uid,
            right.st_gid,
        )
    )
