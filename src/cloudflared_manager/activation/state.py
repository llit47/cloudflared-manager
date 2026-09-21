"""Fixed restrictive state directories and exact ephemeral rollback backups."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from cloudflared_manager.activation.filesystem import (
    FileFacts,
    FilesystemRefused,
    PinnedDirectory,
    fsync_directory,
    named_file,
)
from cloudflared_manager.cloudflared.limits import MAX_CLOUDFLARED_CONFIG_BYTES


def open_fixed_state_child(parent_path: Path, leaf: str, *, anchor: Path = Path("/"),
                           owner: int = 0, create: bool = True) -> PinnedDirectory:
    """Create a known root-owned 0700 child once, with durable parent entry."""

    if leaf not in {"activation-journal", "activation-backups"}:
        raise FilesystemRefused("UNSAFE_STATE_DIRECTORY")
    # A first installation has no manager config root yet. Create only this
    # fixed root-owned parent; never repair an existing unsafe object.
    try:
        parent_path.lstat()
    except FileNotFoundError:
        if not create:
            raise FilesystemRefused("UNSAFE_STATE_DIRECTORY") from None
        with PinnedDirectory(parent_path.parent, anchor=anchor, owner=owner) as grandparent:
            try:
                os.mkdir(parent_path.name, 0o750, dir_fd=grandparent.fd)
            except FileExistsError:
                pass
            except OSError:
                raise FilesystemRefused("UNSAFE_STATE_DIRECTORY") from None
            fsync_directory(grandparent)
    with PinnedDirectory(parent_path, anchor=anchor, owner=owner) as parent:
        if create:
            try:
                os.mkdir(leaf, 0o700, dir_fd=parent.fd)
            except FileExistsError:
                pass
            except OSError:
                raise FilesystemRefused("UNSAFE_STATE_DIRECTORY") from None
            fsync_directory(parent)
    directory = PinnedDirectory(parent_path / leaf, anchor=anchor, owner=owner)
    if directory.facts.mode != 0o700:
        directory.close()
        raise FilesystemRefused("UNSAFE_STATE_DIRECTORY")
    return directory


class BackupStore:
    """Create and authenticate a single restrictive backup in a fixed directory."""

    def __init__(self, directory: PinnedDirectory, *, owner: int = 0) -> None:
        if directory.facts.uid != owner or directory.facts.mode != 0o700:
            raise FilesystemRefused("UNSAFE_BACKUP_DIRECTORY")
        self.directory = directory
        self.owner = owner

    def require_empty(self) -> None:
        """An unjournaled backup has no authenticated deletion authority."""

        self.directory.revalidate()
        try:
            with os.scandir(self.directory.fd) as entries:
                for _ in entries:
                    raise FilesystemRefused("ORPHAN_BACKUP_REQUIRES_REVIEW")
        except OSError:
            raise FilesystemRefused("UNSAFE_BACKUP_DIRECTORY") from None
        fsync_directory(self.directory)

    def create(self, name: str, data: bytes, source: FileFacts) -> FileFacts:
        if (not name.startswith("backup-") or len(name) != 39
            or any(c not in "0123456789abcdef" for c in name[7:])
            or len(data) > MAX_CLOUDFLARED_CONFIG_BYTES):
            raise FilesystemRefused("UNSAFE_BACKUP")
        fd: int | None = None
        created: tuple[int, int] | None = None
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=self.directory.fd)
            meta = os.fstat(fd)
            created = (meta.st_dev, meta.st_ino)
            if (not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_uid != self.owner
                or stat.S_IMODE(meta.st_mode) != 0o600):
                raise FilesystemRefused("UNSAFE_BACKUP")
            remaining = memoryview(data)
            while remaining:
                count = os.write(fd, remaining)
                if count <= 0 or count > len(remaining):
                    raise FilesystemRefused("BACKUP_WRITE_FAILED")
                remaining = remaining[count:]
            os.fsync(fd)
            os.close(fd)
            fd = None
            fsync_directory(self.directory)
            facts, copied = named_file(self.directory, name)
            if (facts.uid != self.owner or facts.mode != 0o600 or facts.sha256 != source.sha256
                or facts.size != source.size or copied != data):
                raise FilesystemRefused("BACKUP_MISMATCH")
            return facts
        except Exception as original:
            cleanup_failure = fd is not None and created is None
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    cleanup_failure = True
            # Failed pre-journal creation is never recovery authority. Remove only
            # the inode allocated here, then durably retire its name.
            if created is not None:
                try:
                    current = os.stat(name, dir_fd=self.directory.fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != created:
                        cleanup_failure = True
                    else:
                        os.unlink(name, dir_fd=self.directory.fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    cleanup_failure = True
                try:
                    fsync_directory(self.directory)
                except FilesystemRefused:
                    cleanup_failure = True
            code = original.code if isinstance(original, FilesystemRefused) else "BACKUP_WRITE_FAILED"
            if cleanup_failure:
                raise FilesystemRefused("BACKUP_CLEANUP_FAILED", original_code=code) from None
            raise FilesystemRefused(code) from None

    def require(self, name: str, expected: FileFacts) -> None:
        facts, _ = named_file(self.directory, name)
        if not facts.same_content_metadata(expected) or facts.mode != 0o600 or facts.uid != self.owner:
            raise FilesystemRefused("BACKUP_MISMATCH")


def unlink_known(directory: PinnedDirectory, name: str, expected: FileFacts, *,
                 missing_ok: bool = False, exchanged: bool = False) -> None:
    """Idempotent cleanup of one authenticated artifact, followed by dir fsync."""

    try:
        current, _ = named_file(directory, name)
    except FilesystemRefused as error:
        try:
            os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                fsync_directory(directory)
                return
        raise error
    if not (current.same_after_exchange(expected) if exchanged
            else current.same_content_metadata(expected)):
        raise FilesystemRefused("ARTIFACT_MISMATCH")
    directory.revalidate()
    os.unlink(name, dir_fd=directory.fd)
    fsync_directory(directory)
