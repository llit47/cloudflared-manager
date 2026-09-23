"""Descriptor-bound Linux filesystem checks for the internal activation engine."""

from __future__ import annotations

import ctypes
import fcntl
import hashlib
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from cloudflared_manager.cloudflared.limits import MAX_CLOUDFLARED_CONFIG_BYTES

if TYPE_CHECKING:
    from cloudflared_manager.cloudflared.editing.source import ConfigSourceSnapshot

_NOFOLLOW = os.O_NOFOLLOW | os.O_CLOEXEC
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | _NOFOLLOW
_FS_IOC_GETFLAGS = 0x80086601
_FS_EXTENT_FL = 0x00080000
_RENAME_EXCHANGE = 2
_LIBC = ctypes.CDLL(None, use_errno=True)


class FilesystemRefused(Exception):
    """A sanitized refusal; underlying paths or file data are never rendered."""

    def __init__(self, code: str, *, original_code: str | None = None) -> None:
        self.code = code
        self.original_code = original_code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class FileFacts:
    device: int
    inode: int
    uid: int
    gid: int
    mode: int
    links: int
    size: int
    sha256: str
    mtime_ns: int
    ctime_ns: int

    def record(self) -> dict[str, int | str]:
        return asdict(self)

    @classmethod
    def parse(cls, value: object) -> FileFacts:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise FilesystemRefused("UNSAFE_JOURNAL")
        for key in (set(value) - {"sha256"}):
            if type(value[key]) is not int or value[key] < 0:
                raise FilesystemRefused("UNSAFE_JOURNAL")
        digest = value["sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise FilesystemRefused("UNSAFE_JOURNAL")
        if (value["inode"] == 0 or value["links"] != 1
            or value["mode"] > 0o777 or value["size"] > MAX_CLOUDFLARED_CONFIG_BYTES):
            raise FilesystemRefused("UNSAFE_JOURNAL")
        return cls(**value)

    def same_content_metadata(self, other: FileFacts) -> bool:
        """Exact journaled file identity, including both change timestamps."""

        return self == other

    def same_after_exchange(self, other: FileFacts) -> bool:
        """Identity after RENAME_EXCHANGE, which changes ctime on Linux.

        Rename preserves mtime and the remaining recorded facts. Use this
        only while an exchange may have occurred but its resulting ctime has
        not yet been durably journaled. Later phases compare the observed
        post-exchange ctime as well. Source, candidate, backup, and staging
        checks before exchange stay exact.
        """

        return (
            self.device, self.inode, self.uid, self.gid, self.mode,
            self.links, self.size, self.sha256, self.mtime_ns,
        ) == (
            other.device, other.inode, other.uid, other.gid, other.mode,
            other.links, other.size, other.sha256, other.mtime_ns,
        )


@dataclass(frozen=True, slots=True)
class DirectoryFacts:
    device: int
    inode: int
    uid: int
    gid: int
    mode: int

    @classmethod
    def from_stat(cls, info: os.stat_result) -> DirectoryFacts:
        if not stat.S_ISDIR(info.st_mode):
            raise FilesystemRefused("UNSAFE_DIRECTORY")
        return cls(info.st_dev, info.st_ino, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))


class PinnedDirectory:
    """Hold and rewalk every trusted directory component before a mutation."""

    def __init__(self, path: Path, *, anchor: Path = Path("/"), owner: int = 0) -> None:
        if not path.is_absolute() or not anchor.is_absolute():
            raise FilesystemRefused("UNSAFE_DIRECTORY")
        try:
            parts = path.relative_to(anchor).parts
        except ValueError as error:
            raise FilesystemRefused("UNSAFE_DIRECTORY") from error
        self.path = path
        self.anchor = anchor
        self.owner = owner
        self._names = parts
        self._fds: list[int] = []
        self._facts: list[DirectoryFacts] = []
        try:
            root_fd = os.open(anchor, _DIR_FLAGS)
            self._append(root_fd)
            for name in parts:
                child_fd = os.open(name, _DIR_FLAGS, dir_fd=self._fds[-1])
                self._append(child_fd)
            self.revalidate()
        except Exception:
            self.close()
            raise FilesystemRefused("UNSAFE_DIRECTORY") from None

    def _append(self, fd: int) -> None:
        try:
            facts = DirectoryFacts.from_stat(os.fstat(fd))
            if facts.uid != self.owner or facts.mode & 0o022:
                raise FilesystemRefused("UNSAFE_DIRECTORY")
            self._fds.append(fd)
            self._facts.append(facts)
        except Exception:
            os.close(fd)
            raise

    @property
    def fd(self) -> int:
        return self._fds[-1]

    @property
    def facts(self) -> DirectoryFacts:
        return self._facts[-1]

    def revalidate(self) -> None:
        """Compare held descriptors with a fresh no-follow walk of their names."""

        walk: list[int] = []
        try:
            root_fd = os.open(self.anchor, _DIR_FLAGS)
            walk.append(root_fd)
            for index, name in enumerate(self._names, start=1):
                walk.append(os.open(name, _DIR_FLAGS, dir_fd=walk[-1]))
                visible = DirectoryFacts.from_stat(os.stat(name, dir_fd=walk[-2], follow_symlinks=False))
                if visible != self._facts[index]:
                    raise FilesystemRefused("STALE_DIRECTORY")
            for current, expected, held in zip(walk, self._facts, self._fds, strict=True):
                if (DirectoryFacts.from_stat(os.fstat(current)) != expected
                    or DirectoryFacts.from_stat(os.fstat(held)) != expected):
                    raise FilesystemRefused("STALE_DIRECTORY")
        except FilesystemRefused:
            raise
        except OSError as error:
            raise FilesystemRefused("STALE_DIRECTORY") from error
        finally:
            for fd in reversed(walk):
                os.close(fd)

    def close(self) -> None:
        while self._fds:
            os.close(self._fds.pop())

    def __enter__(self) -> PinnedDirectory:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _metadata_supported(fd: int) -> None:
    try:
        if os.listxattr(fd):
            raise FilesystemRefused("UNSUPPORTED_METADATA")
        flags = bytearray(4)
        fcntl.ioctl(fd, _FS_IOC_GETFLAGS, flags, True)
        value = int.from_bytes(flags, "little")
        if value & ~_FS_EXTENT_FL:
            raise FilesystemRefused("UNSUPPORTED_METADATA")
    except FilesystemRefused:
        raise
    except OSError as error:
        raise FilesystemRefused("UNSUPPORTED_METADATA") from error


def file_facts(fd: int, *, limit: int = MAX_CLOUDFLARED_CONFIG_BYTES) -> tuple[FileFacts, bytes]:
    """Bounded read with pre/post stat and conservative metadata inventory."""

    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_size < 0 or before.st_size > limit
            or before.st_mode & 0o7000):
            raise FilesystemRefused("UNSAFE_FILE")
        _metadata_supported(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        parts: list[bytes] = []
        remaining = limit + 1
        while remaining:
            part = os.read(fd, min(65_536, remaining))
            if not part:
                break
            parts.append(part)
            remaining -= len(part)
        if not remaining:
            raise FilesystemRefused("UNSAFE_FILE")
        data = b"".join(parts)
        after = os.fstat(fd)
        _metadata_supported(fd)
        comparable = ("st_dev", "st_ino", "st_uid", "st_gid", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, key) != getattr(after, key) for key in comparable) or len(data) != after.st_size:
            raise FilesystemRefused("STALE_FILE")
        facts = FileFacts(after.st_dev, after.st_ino, after.st_uid, after.st_gid,
                          stat.S_IMODE(after.st_mode), after.st_nlink, len(data),
                          hashlib.sha256(data).hexdigest(), after.st_mtime_ns, after.st_ctime_ns)
        return facts, data
    except FilesystemRefused:
        raise
    except OSError as error:
        raise FilesystemRefused("UNSAFE_FILE") from error


def named_file(directory: PinnedDirectory, name: str) -> tuple[FileFacts, bytes]:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise FilesystemRefused("UNSAFE_NAME")
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=directory.fd)
        try:
            facts, data = file_facts(fd)
            visible = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
            if (not stat.S_ISREG(visible.st_mode)
                or (visible.st_dev, visible.st_ino, visible.st_uid, visible.st_gid,
                    stat.S_IMODE(visible.st_mode), visible.st_nlink, visible.st_size,
                    visible.st_mtime_ns, visible.st_ctime_ns)
                != (facts.device, facts.inode, facts.uid, facts.gid, facts.mode,
                    facts.links, facts.size, facts.mtime_ns, facts.ctime_ns)):
                raise FilesystemRefused("STALE_FILE")
            directory.revalidate()
            return facts, data
        finally:
            os.close(fd)
    except FilesystemRefused:
        raise
    except OSError as error:
        raise FilesystemRefused("UNSAFE_FILE") from error


def require_source(snapshot: ConfigSourceSnapshot, directory: PinnedDirectory) -> FileFacts:
    facts, data = named_file(directory, snapshot.path.name)
    if (
        directory.path != snapshot.path.parent
        or directory.facts.device != snapshot.parent_device
        or directory.facts.inode != snapshot.parent_inode
        or directory.facts.uid != snapshot.parent_uid
        or directory.facts.gid != snapshot.parent_gid
        or directory.facts.mode != snapshot.parent_mode
        or facts.device != snapshot.device
        or facts.inode != snapshot.inode
        or facts.uid != snapshot.uid
        or facts.gid != snapshot.gid
        or facts.mode != snapshot.permission_mode
        or facts.size != snapshot.size
        or facts.sha256 != snapshot.sha256
        or facts.mtime_ns != snapshot.mtime_ns
        or facts.ctime_ns != snapshot.ctime_ns
        or data != snapshot.original_bytes
    ):
        raise FilesystemRefused("STALE_SOURCE")
    return facts


def fsync_directory(directory: PinnedDirectory) -> None:
    try:
        directory.revalidate()
        os.fsync(directory.fd)
        directory.revalidate()
    except OSError as error:
        raise FilesystemRefused("DURABILITY_FAILED") from error


def exchange(directory: PinnedDirectory, first: str, second: str) -> None:
    """Same-directory exchange; caller must inspect both displaced identities."""

    for name in (first, second):
        if not name or "/" in name or name in {".", ".."}:
            raise FilesystemRefused("UNSAFE_NAME")
    directory.revalidate()
    rename = getattr(_LIBC, "renameat2", None)
    if rename is None:
        raise FilesystemRefused("EXCHANGE_UNAVAILABLE")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(directory.fd, os.fsencode(first), directory.fd, os.fsencode(second), _RENAME_EXCHANGE) != 0:
        error = ctypes.get_errno()
        raise FilesystemRefused("EXCHANGE_FAILED") from OSError(error, os.strerror(error))
