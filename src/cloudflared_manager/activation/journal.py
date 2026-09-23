"""Fixed-leaf, fsynced activation journal; staging is never recovery authority."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from cloudflared_manager.activation.filesystem import (
    DirectoryFacts,
    FileFacts,
    FilesystemRefused,
    PinnedDirectory,
    fsync_directory,
)

_MAX_RECORD = 16_384
_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CANDIDATE = re.compile(r"^\.cfm-candidate-[0-9a-f]{32}\.yaml$")
_BACKUP = re.compile(r"^backup-[0-9a-f]{32}$")
_FINAL = frozenset({"PRECOMMIT_ABORT", "COMMIT_CLEANUP_PENDING", "ROLLBACK_CLEANUP_PENDING"})
_TRANSITIONS = {
    "BACKUP_DURABLE": {"CONFIG_COMMITTING", "PRECOMMIT_ABORT"},
    "CONFIG_COMMITTING": {"CONFIG_COMMITTED", "PRECOMMIT_ABORT", "ACTIVATION_FAILED"},
    "CONFIG_COMMITTED": {"SERVICE_ACTIVATING", "ACTIVATION_FAILED"},
    "SERVICE_ACTIVATING": {"SERVICE_VERIFIED", "ACTIVATION_FAILED"},
    "SERVICE_VERIFIED": {"COMMIT_CLEANUP_PENDING", "ACTIVATION_FAILED"},
    "ACTIVATION_FAILED": {"ROLLBACK_CONFIG"},
    "ROLLBACK_CONFIG": {"ROLLBACK_CONFIG", "ROLLBACK_SERVICE"},
    "ROLLBACK_SERVICE": {"ROLLBACK_VERIFIED"},
    "ROLLBACK_VERIFIED": {"ROLLBACK_CLEANUP_PENDING"},
}


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise FilesystemRefused("UNSAFE_JOURNAL")
        result[key] = value
    return result


def _parse_ctimes(value: object) -> tuple[int, int] | None:
    if value is None:
        return None
    if (not isinstance(value, list) or len(value) != 2
        or any(type(item) is not int or item < 0 for item in value)):
        raise ValueError
    return value[0], value[1]


@dataclass(frozen=True, slots=True)
class BaselineFacts:
    """Sanitized evidence supplied by a future independently verified service gate."""

    unit: str
    load_state: str
    active_state: str
    sub_state: str
    main_pid: int
    process_start_ticks: int
    executable_device: int
    executable_inode: int
    adopted_fingerprint: str
    source_digest: str
    stable_milliseconds: int
    restart_counter: int

    def record(self) -> dict[str, int | str]:
        from dataclasses import asdict
        return asdict(self)

    @classmethod
    def parse(cls, value: object) -> BaselineFacts:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise FilesystemRefused("UNSAFE_JOURNAL")
        for key in ("main_pid", "process_start_ticks", "executable_device", "executable_inode", "stable_milliseconds"):
            if type(value[key]) is not int or value[key] <= 0:
                raise FilesystemRefused("UNSAFE_JOURNAL")
        if type(value["restart_counter"]) is not int or value["restart_counter"] < 0:
            raise FilesystemRefused("UNSAFE_JOURNAL")
        if (value["unit"] != "cloudflared.service" or value["load_state"] != "loaded"
            or value["active_state"] != "active" or value["sub_state"] != "running"):
            raise FilesystemRefused("UNSAFE_JOURNAL")
        for key in ("adopted_fingerprint", "source_digest"):
            if not isinstance(value[key], str) or not _DIGEST.fullmatch(value[key]):
                raise FilesystemRefused("UNSAFE_JOURNAL")
        return cls(**value)


@dataclass(frozen=True, slots=True, repr=False)
class JournalRecord:
    version: int
    transaction_id: str
    generation: int
    predecessor_generation: int | None
    predecessor_digest: str | None
    predecessor_phase: str | None
    phase: str
    release_id: str
    adopted_fingerprint: str
    source: FileFacts
    candidate: FileFacts
    backup: FileFacts
    parent: DirectoryFacts
    candidate_name: str
    backup_name: str
    baseline: BaselineFacts
    cleanup: dict[str, FileFacts]
    restoration: FileFacts | None = None
    commit_ctimes: tuple[int, int] | None = None
    rollback_ctimes: tuple[int, int] | None = None

    def __repr__(self) -> str:
        return f"JournalRecord(phase={self.phase!r}, generation={self.generation})"

    @property
    def restoration_name(self) -> str:
        return f".cfm-restore-{self.transaction_id}.yaml"

    def bytes(self) -> bytes:
        payload = {
            "version": self.version,
            "transaction_id": self.transaction_id,
            "generation": self.generation,
            "predecessor_generation": self.predecessor_generation,
            "predecessor_digest": self.predecessor_digest,
            "predecessor_phase": self.predecessor_phase,
            "phase": self.phase,
            "release_id": self.release_id,
            "adopted_fingerprint": self.adopted_fingerprint,
            "source": self.source.record(),
            "candidate": self.candidate.record(),
            "backup": self.backup.record(),
            "parent": {
                "device": self.parent.device, "inode": self.parent.inode,
                "uid": self.parent.uid, "gid": self.parent.gid, "mode": self.parent.mode,
            },
            "candidate_name": self.candidate_name,
            "backup_name": self.backup_name,
            "baseline": self.baseline.record(),
            "cleanup": {name: facts.record() for name, facts in self.cleanup.items()},
            "restoration": self.restoration.record() if self.restoration is not None else None,
            "commit_ctimes": self.commit_ctimes,
            "rollback_ctimes": self.rollback_ctimes,
        }
        result = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        if len(result) > _MAX_RECORD:
            raise FilesystemRefused("UNSAFE_JOURNAL")
        return result

    @classmethod
    def parse(cls, raw: bytes) -> JournalRecord:
        if len(raw) > _MAX_RECORD:
            raise FilesystemRefused("UNSAFE_JOURNAL")
        try:
            payload = json.loads(raw.decode("ascii"), object_pairs_hook=_object_pairs)
            if not isinstance(payload, dict) or set(payload) != set(cls.__dataclass_fields__):
                raise ValueError
            if type(payload["version"]) is not int or payload["version"] != 1:
                raise ValueError
            if type(payload["generation"]) is not int or payload["generation"] < 1:
                raise ValueError
            if not isinstance(payload["transaction_id"], str) or not _ID.fullmatch(payload["transaction_id"]):
                raise ValueError
            if not isinstance(payload["release_id"], str) or not _SHA.fullmatch(payload["release_id"]):
                raise ValueError
            if not isinstance(payload["adopted_fingerprint"], str) or not _DIGEST.fullmatch(payload["adopted_fingerprint"]):
                raise ValueError
            if payload["phase"] not in set(_TRANSITIONS) | _FINAL:
                raise ValueError
            if not isinstance(payload["candidate_name"], str) or not _CANDIDATE.fullmatch(payload["candidate_name"]):
                raise ValueError
            if not isinstance(payload["backup_name"], str) or not _BACKUP.fullmatch(payload["backup_name"]):
                raise ValueError
            if payload["generation"] == 1:
                if (payload["phase"] != "BACKUP_DURABLE" or payload["predecessor_generation"] is not None
                    or payload["predecessor_digest"] is not None or payload["predecessor_phase"] is not None):
                    raise ValueError
            else:
                if (type(payload["predecessor_generation"]) is not int
                    or payload["predecessor_generation"] != payload["generation"] - 1
                    or not isinstance(payload["predecessor_digest"], str)
                    or not _DIGEST.fullmatch(payload["predecessor_digest"])
                    or not isinstance(payload["predecessor_phase"], str)
                    or payload["phase"] not in _TRANSITIONS.get(payload["predecessor_phase"], set())):
                    raise ValueError
            parent = payload["parent"]
            if not isinstance(parent, dict) or set(parent) != set(DirectoryFacts.__dataclass_fields__):
                raise ValueError
            if any(type(value) is not int or value < 0 for value in parent.values()):
                raise ValueError
            baseline = BaselineFacts.parse(payload["baseline"])
            cleanup = payload["cleanup"]
            if not isinstance(cleanup, dict) or set(cleanup) - {"candidate", "backup"}:
                raise ValueError
            if payload["phase"] in _FINAL and set(cleanup) != {"candidate", "backup"}:
                raise ValueError
            if payload["phase"] not in _FINAL and cleanup:
                raise ValueError
            record = cls(
                version=1,
                transaction_id=payload["transaction_id"],
                generation=payload["generation"],
                predecessor_generation=payload["predecessor_generation"],
                predecessor_digest=payload["predecessor_digest"],
                predecessor_phase=payload["predecessor_phase"],
                phase=payload["phase"],
                release_id=payload["release_id"],
                adopted_fingerprint=payload["adopted_fingerprint"],
                source=FileFacts.parse(payload["source"]),
                candidate=FileFacts.parse(payload["candidate"]),
                backup=FileFacts.parse(payload["backup"]),
                parent=DirectoryFacts(**parent),
                candidate_name=payload["candidate_name"],
                backup_name=payload["backup_name"],
                baseline=baseline,
                cleanup={name: FileFacts.parse(value) for name, value in cleanup.items()},
                restoration=(FileFacts.parse(payload["restoration"])
                             if payload["restoration"] is not None else None),
                commit_ctimes=_parse_ctimes(payload["commit_ctimes"]),
                rollback_ctimes=_parse_ctimes(payload["rollback_ctimes"]),
            )
            if (
                record.source.device != record.parent.device
                or record.candidate.device != record.parent.device
                or record.source.inode == record.candidate.inode
                or (record.source.uid, record.source.gid, record.source.mode)
                != (record.candidate.uid, record.candidate.gid, record.candidate.mode)
                or (record.backup.sha256, record.backup.size, record.backup.mode)
                != (record.source.sha256, record.source.size, 0o600)
                or record.source.sha256 == record.candidate.sha256
                or record.baseline.source_digest != record.source.sha256
                or record.baseline.adopted_fingerprint != record.adopted_fingerprint
                or record.parent.mode & 0o022
            ):
                raise ValueError
            if record.restoration is not None:
                restored = record.restoration
                if (record.phase not in {"ROLLBACK_CONFIG", "ROLLBACK_SERVICE", "ROLLBACK_VERIFIED", "ROLLBACK_CLEANUP_PENDING"}
                    or record.predecessor_phase not in {"ROLLBACK_CONFIG", "ROLLBACK_SERVICE", "ROLLBACK_VERIFIED"}
                    or restored.device != record.parent.device
                    # The original has been unlinked before backup staging;
                    # the filesystem may legitimately reuse its inode number.
                    or restored.inode == record.candidate.inode
                    or (restored.uid, restored.gid, restored.mode, restored.size, restored.sha256)
                    != (record.source.uid, record.source.gid, record.source.mode,
                        record.source.size, record.source.sha256)):
                    raise ValueError
            if (record.phase in {"CONFIG_COMMITTED", "SERVICE_ACTIVATING", "SERVICE_VERIFIED", "COMMIT_CLEANUP_PENDING"}
                and record.commit_ctimes is None):
                raise ValueError
            if (record.commit_ctimes is not None and record.phase in {"BACKUP_DURABLE", "CONFIG_COMMITTING", "PRECOMMIT_ABORT"}):
                raise ValueError
            if (record.rollback_ctimes is not None
                and record.phase not in {"ROLLBACK_CONFIG", "ROLLBACK_SERVICE", "ROLLBACK_VERIFIED", "ROLLBACK_CLEANUP_PENDING"}):
                raise ValueError
            if (record.phase in {"ROLLBACK_SERVICE", "ROLLBACK_VERIFIED", "ROLLBACK_CLEANUP_PENDING"}
                and record.rollback_ctimes is None):
                raise ValueError
            if (record.phase == record.predecessor_phase == "ROLLBACK_CONFIG"
                and record.restoration is None and record.rollback_ctimes is None):
                raise ValueError
            if record.phase in _FINAL:
                expected_candidate = (
                    record.source if record.phase == "COMMIT_CLEANUP_PENDING"
                    else record.candidate
                )
                if record.cleanup != {"candidate": expected_candidate, "backup": record.backup}:
                    raise ValueError
            if record.bytes() != raw:
                raise ValueError
            return record
        except (UnicodeError, ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError) as error:
            raise FilesystemRefused("UNSAFE_JOURNAL") from None

    def successor(self, phase: str, *, cleanup: dict[str, FileFacts] | None = None,
                  restoration: FileFacts | None = None,
                  commit_ctimes: tuple[int, int] | None = None,
                  rollback_ctimes: tuple[int, int] | None = None) -> JournalRecord:
        if phase not in _TRANSITIONS.get(self.phase, set()):
            raise FilesystemRefused("INVALID_TRANSITION")
        if phase == self.phase == "ROLLBACK_CONFIG":
            staging = self.restoration is None and restoration is not None and rollback_ctimes is None
            completing = (self.rollback_ctimes is None and rollback_ctimes is not None
                          and (restoration is None or restoration == self.restoration))
            if not (staging or completing):
                raise FilesystemRefused("INVALID_TRANSITION")
        elif self.phase == "ACTIVATION_FAILED" and restoration is not None:
            raise FilesystemRefused("INVALID_TRANSITION")
        elif restoration is not None and restoration != self.restoration:
            raise FilesystemRefused("INVALID_TRANSITION")
        if phase == "CONFIG_COMMITTED" and self.phase == "CONFIG_COMMITTING":
            if commit_ctimes is None or self.commit_ctimes is not None:
                raise FilesystemRefused("INVALID_TRANSITION")
        elif commit_ctimes is not None and commit_ctimes != self.commit_ctimes:
            raise FilesystemRefused("INVALID_TRANSITION")
        if phase != self.phase or phase != "ROLLBACK_CONFIG":
            if rollback_ctimes is not None and rollback_ctimes != self.rollback_ctimes:
                raise FilesystemRefused("INVALID_TRANSITION")
        next_restoration = restoration if restoration is not None else self.restoration
        next_commit_ctimes = commit_ctimes if commit_ctimes is not None else self.commit_ctimes
        next_rollback_ctimes = rollback_ctimes if rollback_ctimes is not None else self.rollback_ctimes
        next_cleanup = {} if cleanup is None else cleanup
        if (phase in _FINAL) != (set(next_cleanup) == {"candidate", "backup"}):
            raise FilesystemRefused("INVALID_TRANSITION")
        return replace(self, generation=self.generation + 1,
                       predecessor_generation=self.generation,
                       predecessor_digest=hashlib.sha256(self.bytes()).hexdigest(),
                       predecessor_phase=self.phase,
                       phase=phase, cleanup=next_cleanup, restoration=next_restoration,
                       commit_ctimes=next_commit_ctimes, rollback_ctimes=next_rollback_ctimes)


class JournalStore:
    """Own the fixed two-leaf namespace through a verified directory descriptor."""

    def __init__(self, directory: PinnedDirectory, *, owner: int = 0) -> None:
        if directory.facts.uid != owner or directory.facts.mode != 0o700:
            raise FilesystemRefused("UNSAFE_JOURNAL_DIRECTORY")
        self.directory = directory
        self.owner = owner

    def _names(self) -> set[str]:
        self.directory.revalidate()
        try:
            names: set[str] = set()
            with os.scandir(self.directory.fd) as entries:
                for entry in entries:
                    if entry.name not in {"journal", "journal.next"} or len(names) >= 2:
                        raise FilesystemRefused("UNSAFE_JOURNAL_NAMESPACE")
                    names.add(entry.name)
        except OSError:
            raise FilesystemRefused("UNSAFE_JOURNAL_NAMESPACE") from None
        self.directory.revalidate()
        return names

    def _read(self, name: str, *, parse: bool) -> tuple[JournalRecord | None, bytes, os.stat_result]:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.directory.fd)
            try:
                before = os.fstat(fd)
                if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_uid != self.owner or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_size > _MAX_RECORD or before.st_size < 0):
                    raise FilesystemRefused("UNSAFE_JOURNAL")
                raw = os.read(fd, _MAX_RECORD + 1)
                after = os.fstat(fd)
                visible = os.stat(name, dir_fd=self.directory.fd, follow_symlinks=False)
                if (len(raw) != before.st_size or len(raw) > _MAX_RECORD
                    or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or (visible.st_dev, visible.st_ino) != (after.st_dev, after.st_ino)):
                    raise FilesystemRefused("UNSAFE_JOURNAL")
                self.directory.revalidate()
                return (JournalRecord.parse(raw) if parse else None), raw, after
            finally:
                os.close(fd)
        except FilesystemRefused:
            raise
        except OSError:
            raise FilesystemRefused("UNSAFE_JOURNAL") from None

    def load(self) -> JournalRecord | None:
        names = self._names()
        if "journal" not in names:
            return None
        record, _, _ = self._read("journal", parse=True)
        return record

    def recover_staging(
        self,
        *,
        authenticate: Callable[[JournalRecord], None] | None = None,
    ) -> JournalRecord | None:
        names = self._names()
        record: JournalRecord | None = None
        published_bytes: bytes | None = None
        published_stat: os.stat_result | None = None
        if "journal" in names:
            record, published_bytes, published_stat = self._read("journal", parse=True)
        if record is not None and "journal.next" in names:
            # A parsed journal is not yet recovery authority. Its release,
            # adopted path, and phase-dependent artifacts must authenticate
            # before even an unpublished staging leaf can be removed.
            if authenticate is None:
                raise FilesystemRefused("RECOVERY_REQUIRED")
            authenticate(record)
            self._require_same_published(published_bytes, published_stat)
        if "journal.next" in names:
            _, _, staged = self._read("journal.next", parse=False)
            visible = os.stat("journal.next", dir_fd=self.directory.fd, follow_symlinks=False)
            if (visible.st_dev, visible.st_ino) != (staged.st_dev, staged.st_ino):
                raise FilesystemRefused("UNSAFE_JOURNAL")
            os.unlink("journal.next", dir_fd=self.directory.fd)
            fsync_directory(self.directory)
        if record is not None:
            if authenticate is not None:
                authenticate(record)
            fsync_directory(self.directory)
            self._require_same_published(published_bytes, published_stat)
            if "journal.next" in self._names():
                raise FilesystemRefused("UNSAFE_JOURNAL")
            return record
        self.require_clean()
        return None

    def require_clean(self) -> None:
        if self._names():
            raise FilesystemRefused("RECOVERY_REQUIRED")
        fsync_directory(self.directory)
        if self._names():
            raise FilesystemRefused("JOURNAL_NAMESPACE_NOT_DURABLY_CLEAN")

    def _require_same_published(self, raw: bytes | None, info: os.stat_result | None) -> None:
        if raw is None or info is None:
            raise FilesystemRefused("UNSAFE_JOURNAL")
        _, current_raw, current = self._read("journal", parse=True)
        fields = ("st_dev", "st_ino", "st_uid", "st_gid", "st_mode", "st_nlink",
                  "st_size", "st_mtime_ns", "st_ctime_ns")
        if current_raw != raw or any(getattr(current, key) != getattr(info, key) for key in fields):
            raise FilesystemRefused("UNSAFE_JOURNAL")

    def publish(
        self,
        new: JournalRecord,
        *,
        authenticate: Callable[[JournalRecord], None],
        authenticate_complete_cleanup: Callable[[JournalRecord], None] | None = None,
    ) -> JournalRecord:
        current = self.load()
        current_raw: bytes | None = None
        current_stat: os.stat_result | None = None
        if current is None:
            self.require_clean()
            if new.generation != 1 or new.phase != "BACKUP_DURABLE":
                raise FilesystemRefused("INVALID_TRANSITION")
        else:
            _, current_raw, current_stat = self._read("journal", parse=True)
            authenticate(current)
            if new != current.successor(new.phase, cleanup=new.cleanup,
                                        restoration=new.restoration,
                                        commit_ctimes=new.commit_ctimes,
                                        rollback_ctimes=new.rollback_ctimes):
                raise FilesystemRefused("INVALID_TRANSITION")
        first_cleanup_decision = new.phase in _FINAL
        if first_cleanup_decision:
            if authenticate_complete_cleanup is None:
                raise FilesystemRefused("INVALID_TRANSITION")
            authenticate_complete_cleanup(new)
        if "journal.next" in self._names():
            raise FilesystemRefused("RECOVERY_REQUIRED")
        raw = new.bytes()
        fd: int | None = None
        try:
            fd = os.open("journal.next", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=self.directory.fd)
            metadata = os.fstat(fd)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != self.owner
                or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600):
                raise FilesystemRefused("UNSAFE_JOURNAL")
            remaining = memoryview(raw)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0 or written > len(remaining):
                    raise FilesystemRefused("JOURNAL_PUBLICATION_FAILED")
                remaining = remaining[written:]
            os.fsync(fd)
            os.close(fd)
            fd = None
            staged, staged_bytes, _ = self._read("journal.next", parse=True)
            if staged != new or staged_bytes != raw:
                raise FilesystemRefused("JOURNAL_PUBLICATION_FAILED")
            authenticate(new)
            if current is None:
                if "journal" in self._names():
                    raise FilesystemRefused("UNSAFE_JOURNAL")
            else:
                self._require_same_published(current_raw, current_stat)
            _, repeated_staging, _ = self._read("journal.next", parse=True)
            if repeated_staging != raw:
                raise FilesystemRefused("UNSAFE_JOURNAL")
            if first_cleanup_decision:
                assert authenticate_complete_cleanup is not None
                authenticate_complete_cleanup(new)
            self.directory.revalidate()
            os.rename("journal.next", "journal", src_dir_fd=self.directory.fd, dst_dir_fd=self.directory.fd)
            fsync_directory(self.directory)
            published, published_bytes, _ = self._read("journal", parse=True)
            if published != new or published_bytes != raw or "journal.next" in self._names():
                raise FilesystemRefused("JOURNAL_PUBLICATION_FAILED")
            authenticate(new)
            if first_cleanup_decision:
                assert authenticate_complete_cleanup is not None
                authenticate_complete_cleanup(new)
            return new
        except FilesystemRefused:
            raise
        except OSError:
            raise FilesystemRefused("JOURNAL_PUBLICATION_FAILED") from None
        finally:
            if fd is not None:
                os.close(fd)

    def retire(self, record: JournalRecord, *, authenticate: Callable[[JournalRecord], None]) -> None:
        if record.phase not in _FINAL:
            raise FilesystemRefused("INVALID_TRANSITION")
        actual, raw, info = self._read("journal", parse=True)
        if actual != record or "journal.next" in self._names():
            raise FilesystemRefused("UNSAFE_JOURNAL")
        authenticate(record)
        self._require_same_published(raw, info)
        if "journal.next" in self._names():
            raise FilesystemRefused("UNSAFE_JOURNAL")
        os.unlink("journal", dir_fd=self.directory.fd)
        fsync_directory(self.directory)
        self.require_clean()
