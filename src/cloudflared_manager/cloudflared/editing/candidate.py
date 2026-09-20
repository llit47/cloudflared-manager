"""Exclusive, same-directory staging for disposable candidate files only."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from collections.abc import Callable
from pathlib import Path

from cloudflared_manager.cloudflared.editing.errors import (
    CandidateFileError,
    SourceConfigChangedError,
)
from cloudflared_manager.cloudflared.editing.source import ConfigSourceSnapshot
from cloudflared_manager.cloudflared.limits import MAX_CLOUDFLARED_CONFIG_BYTES

_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_CREATE_ATTEMPTS = 8


class CandidateFile:
    """A verified staged file that can only be retained or discarded, not activated."""

    __slots__ = (
        "_path",
        "_name",
        "_directory_fd",
        "_device",
        "_inode",
        "_size",
        "_sha256",
        "_discarded",
    )

    def __init__(
        self,
        *,
        path: Path,
        name: str,
        directory_fd: int,
        device: int,
        inode: int,
        size: int,
        sha256: str,
    ) -> None:
        self._path = path
        self._name = name
        self._directory_fd = directory_fd
        self._device = device
        self._inode = inode
        self._size = size
        self._sha256 = sha256
        self._discarded = False

    def __repr__(self) -> str:
        state = "staged" if not self._discarded else "discarded"
        return f"CandidateFile(state={state!r})"

    @property
    def path(self) -> Path:
        """Return the internally generated path required by validators."""

        if self._discarded:
            raise CandidateFileError("The candidate file has already been discarded.")
        return self._path

    def require_intact(self) -> None:
        """Verify the visible name still identifies the staged regular file."""

        if self._discarded:
            raise CandidateFileError("The candidate file has already been discarded.")
        descriptor: int | None = None
        try:
            no_follow = getattr(os, "O_NOFOLLOW", None)
            if no_follow is None:
                raise CandidateFileError(
                    "Safe no-follow candidate verification is unavailable."
                )
            descriptor = os.open(
                self._name,
                os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0),
                dir_fd=self._directory_fd,
            )
            metadata = os.fstat(descriptor)
        except OSError as error:
            raise CandidateFileError(
                "The candidate file is no longer available for validation."
            ) from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != (self._device, self._inode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size != self._size
        ):
            if descriptor is not None:
                os.close(descriptor)
            raise CandidateFileError(
                "The candidate file identity or permissions changed unexpectedly."
            )
        digest = hashlib.sha256()
        remaining = self._size
        try:
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
            trailing = os.read(descriptor, 1)
            final_metadata = os.fstat(descriptor)
        except OSError as error:
            raise CandidateFileError(
                "The candidate file could not be verified safely."
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if (
            remaining
            or trailing
            or digest.hexdigest() != self._sha256
            or (final_metadata.st_dev, final_metadata.st_ino, final_metadata.st_size)
            != (self._device, self._inode, self._size)
        ):
            raise CandidateFileError(
                "The candidate file contents changed unexpectedly."
            )

    def discard(self) -> None:
        """Remove this candidate without touching the source configuration."""

        if self._discarded:
            return
        failure: OSError | CandidateFileError | None = None
        try:
            try:
                metadata = os.stat(
                    self._name,
                    dir_fd=self._directory_fd,
                    follow_symlinks=False,
                )
                if (metadata.st_dev, metadata.st_ino) != (
                    self._device,
                    self._inode,
                ):
                    raise CandidateFileError(
                        "The candidate name no longer identifies the staged file."
                    )
                os.unlink(self._name, dir_fd=self._directory_fd)
            except CandidateFileError as error:
                failure = error
        except OSError as error:
            failure = error
        finally:
            try:
                os.close(self._directory_fd)
            except OSError as error:
                failure = failure or error
            self._discarded = True
        if failure is not None:
            raise CandidateFileError(
                "The candidate file could not be cleaned up safely."
            ) from failure

    def __enter__(self) -> CandidateFile:
        return self

    def __exit__(self, *_: object) -> None:
        self.discard()


class CandidateFileStager:
    """Create restrictive candidates through directory-relative operations."""

    def __init__(
        self,
        *,
        token_factory: Callable[[int], str] = secrets.token_hex,
        write: Callable[[int, bytes | memoryview], int] = os.write,
        fsync: Callable[[int], None] = os.fsync,
        close: Callable[[int], None] = os.close,
        attempts: int = _CREATE_ATTEMPTS,
    ) -> None:
        self._token_factory = token_factory
        self._write = write
        self._fsync = fsync
        self._close = close
        self._attempts = attempts

    def stage(
        self,
        snapshot: ConfigSourceSnapshot,
        contents: bytes,
    ) -> CandidateFile:
        """Write and fsync a unique file beside the unchanged source."""

        if len(contents) > MAX_CLOUDFLARED_CONFIG_BYTES:
            raise CandidateFileError(
                "The candidate exceeds the supported configuration size limit."
            )
        directory_fd = _open_source_directory(snapshot)
        descriptor: int | None = None
        name: str | None = None
        candidate_metadata: os.stat_result | None = None
        closed = False
        try:
            descriptor, name = self._create_unique(directory_fd)
            candidate_metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(candidate_metadata.st_mode)
                or stat.S_IMODE(candidate_metadata.st_mode) != 0o600
                or candidate_metadata.st_nlink != 1
            ):
                raise CandidateFileError(
                    "The candidate file was not created with safe metadata."
                )
            _write_completely(descriptor, contents, self._write)
            self._fsync(descriptor)
            final_metadata = os.fstat(descriptor)
            if final_metadata.st_size != len(contents):
                raise CandidateFileError(
                    "The candidate file was not written completely."
                )
            self._close(descriptor)
            closed = True
            descriptor = None

            staged = CandidateFile(
                path=snapshot.path.parent / name,
                name=name,
                directory_fd=directory_fd,
                device=candidate_metadata.st_dev,
                inode=candidate_metadata.st_ino,
                size=len(contents),
                sha256=hashlib.sha256(contents).hexdigest(),
            )
            staged.require_intact()
            return staged
        except (CandidateFileError, OSError, ValueError) as error:
            if descriptor is not None and not closed:
                try:
                    self._close(descriptor)
                except OSError:
                    pass
            if name is not None and candidate_metadata is not None:
                _unlink_if_same(directory_fd, name, candidate_metadata)
            try:
                os.close(directory_fd)
            except OSError:
                pass
            if isinstance(error, CandidateFileError):
                raise
            raise CandidateFileError(
                "The candidate file could not be created safely."
            ) from error

    def _create_unique(self, directory_fd: int) -> tuple[int, str]:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise CandidateFileError(
                "Safe no-follow candidate creation is unavailable on this platform."
            )
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | no_follow
            | getattr(os, "O_CLOEXEC", 0)
        )
        for _ in range(self._attempts):
            token = self._token_factory(16)
            if not _TOKEN_PATTERN.fullmatch(token):
                raise CandidateFileError(
                    "The internal candidate name generator returned an unsafe value."
                )
            name = f".cfm-candidate-{token}.yaml"
            try:
                return os.open(name, flags, 0o600, dir_fd=directory_fd), name
            except FileExistsError:
                continue
        raise CandidateFileError(
            "A unique candidate file could not be allocated safely."
        )


def _open_source_directory(snapshot: ConfigSourceSnapshot) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory_flag is None:
        raise CandidateFileError(
            "Safe directory-relative candidate creation is unavailable on this platform."
        )
    flags = os.O_RDONLY | directory_flag | no_follow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(snapshot.path.parent, flags)
        metadata = os.fstat(descriptor)
    except OSError as error:
        raise CandidateFileError(
            "The source configuration directory could not be opened safely."
        ) from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino)
        != (snapshot.parent_device, snapshot.parent_inode)
        or stat.S_IMODE(metadata.st_mode) != snapshot.parent_mode
    ):
        os.close(descriptor)
        raise SourceConfigChangedError(
            "The source configuration directory changed during preparation."
        )
    if metadata.st_mode & 0o022:
        os.close(descriptor)
        raise CandidateFileError(
            "The source configuration directory is writable by an untrusted group or user."
        )
    return descriptor


def _write_completely(
    descriptor: int,
    contents: bytes,
    write: Callable[[int, bytes | memoryview], int],
) -> None:
    remaining = memoryview(contents)
    while remaining:
        written = write(descriptor, remaining)
        if written <= 0 or written > len(remaining):
            raise CandidateFileError(
                "The candidate file could not be written completely."
            )
        remaining = remaining[written:]


def _unlink_if_same(
    directory_fd: int,
    name: str,
    expected: os.stat_result,
) -> None:
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino):
            os.unlink(name, dir_fd=directory_fd)
    except OSError:
        pass
