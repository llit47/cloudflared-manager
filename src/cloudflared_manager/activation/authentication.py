"""Authenticate a published recovery record without loading the editing stack."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Protocol

from cloudflared_manager.activation.filesystem import (
    FileFacts, FilesystemRefused, PinnedDirectory, _metadata_supported, named_file,
)
from cloudflared_manager.activation.journal import JournalRecord
from cloudflared_manager.activation.state import BackupStore


class RecoveryAuthority(Protocol):
    def current(self) -> tuple[str, Path]: ...


def _exists(directory: PinnedDirectory, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def authenticate_record(
    record: JournalRecord,
    authority: RecoveryAuthority,
    active: PinnedDirectory,
    backups: BackupStore,
) -> None:
    """Authenticate published authority and all phase-required artifacts."""

    release, adopted = authority.current()
    if (release != record.release_id or adopted.parent != active.path
        or hashlib.sha256(os.fsencode(adopted)).hexdigest() != record.adopted_fingerprint
        or active.facts != record.parent):
        raise FilesystemRefused("STALE_AUTHORITY")
    active.revalidate()
    _metadata_supported(active.fd)

    def matches(current: FileFacts, expected: FileFacts, *, exchanged: bool = False,
                ctime_ns: int | None = None) -> bool:
        identity = (current.same_after_exchange(expected) if exchanged
                    else current.same_content_metadata(expected))
        return identity and (ctime_ns is None or current.ctime_ns == ctime_ns)

    def require_at(name: str, expected: FileFacts, *, exchanged: bool = False,
                   ctime_ns: int | None = None) -> None:
        current, _ = named_file(active, name)
        if not matches(current, expected, exchanged=exchanged, ctime_ns=ctime_ns):
            raise FilesystemRefused("ARTIFACT_MISMATCH")

    if record.phase not in {"PRECOMMIT_ABORT", "COMMIT_CLEANUP_PENDING", "ROLLBACK_CLEANUP_PENDING"}:
        backups.require(record.backup_name, record.backup)
    if record.phase in {"BACKUP_DURABLE", "PRECOMMIT_ABORT"}:
        require_at(adopted.name, record.source)
        if record.phase != "PRECOMMIT_ABORT" or _exists(active, record.candidate_name):
            require_at(record.candidate_name, record.candidate)
    elif record.phase == "CONFIG_COMMITTING":
        current, _ = named_file(active, adopted.name)
        if current.same_content_metadata(record.source):
            require_at(record.candidate_name, record.candidate)
        elif current.same_after_exchange(record.candidate):
            if _exists(active, record.candidate_name):
                require_at(record.candidate_name, record.source, exchanged=True)
        else:
            raise FilesystemRefused("UNKNOWN_ACTIVE_STATE")
    elif record.phase in {"CONFIG_COMMITTED", "ACTIVATION_FAILED", "ROLLBACK_CONFIG", "SERVICE_ACTIVATING", "SERVICE_VERIFIED", "COMMIT_CLEANUP_PENDING"}:
        current, _ = named_file(active, adopted.name)
        restored_active = record.phase in {"ACTIVATION_FAILED", "ROLLBACK_CONFIG"} and current.same_after_exchange(
            record.restoration or record.source
        )
        if restored_active and record.phase == "ROLLBACK_CONFIG" and record.rollback_ctimes is not None:
            if current.ctime_ns != record.rollback_ctimes[0]:
                raise FilesystemRefused("ARTIFACT_MISMATCH")
        if record.phase == "ROLLBACK_CONFIG" and record.rollback_ctimes is not None and not restored_active:
            raise FilesystemRefused("UNKNOWN_ACTIVE_STATE")
        if record.restoration is not None and record.phase == "ROLLBACK_CONFIG":
            if not restored_active and not matches(
                current, record.candidate, exchanged=True,
                ctime_ns=record.commit_ctimes[0] if record.commit_ctimes is not None else None,
            ):
                raise FilesystemRefused("UNKNOWN_ACTIVE_STATE")
            if _exists(active, record.candidate_name):
                raise FilesystemRefused("ARTIFACT_MISMATCH")
            require_at(record.restoration_name,
                       record.candidate if restored_active else record.restoration,
                       exchanged=restored_active,
                       ctime_ns=(record.rollback_ctimes[1] if restored_active and record.rollback_ctimes
                                 is not None else None))
        elif restored_active:
            require_at(record.candidate_name, record.candidate, exchanged=True,
                       ctime_ns=(record.rollback_ctimes[1] if record.phase == "ROLLBACK_CONFIG"
                                 and record.rollback_ctimes is not None else None))
        elif not matches(current, record.candidate, exchanged=True,
                         ctime_ns=record.commit_ctimes[0] if record.commit_ctimes is not None else None):
            raise FilesystemRefused("UNKNOWN_ACTIVE_STATE")
        if not restored_active and record.restoration is None and record.phase in {
            "CONFIG_COMMITTED", "ACTIVATION_FAILED", "ROLLBACK_CONFIG", "SERVICE_ACTIVATING", "SERVICE_VERIFIED"
        }:
            if _exists(active, record.candidate_name):
                require_at(record.candidate_name, record.restoration or record.source, exchanged=True,
                           ctime_ns=record.commit_ctimes[1] if record.commit_ctimes is not None else None)
            elif record.restoration is not None or record.phase in {"SERVICE_ACTIVATING", "SERVICE_VERIFIED"}:
                raise FilesystemRefused("ARTIFACT_MISMATCH")
    elif record.phase in {"ROLLBACK_SERVICE", "ROLLBACK_VERIFIED", "ROLLBACK_CLEANUP_PENDING"}:
        assert record.rollback_ctimes is not None
        require_at(adopted.name, record.restoration or record.source, exchanged=True,
                   ctime_ns=record.rollback_ctimes[0])
        if record.restoration is not None:
            if _exists(active, record.candidate_name):
                raise FilesystemRefused("ARTIFACT_MISMATCH")
            if record.phase != "ROLLBACK_CLEANUP_PENDING":
                require_at(record.restoration_name, record.candidate, exchanged=True,
                           ctime_ns=record.rollback_ctimes[1])
        elif record.phase != "ROLLBACK_CLEANUP_PENDING":
            require_at(record.candidate_name, record.candidate, exchanged=True,
                       ctime_ns=record.rollback_ctimes[1])
    if record.phase in {"PRECOMMIT_ABORT", "COMMIT_CLEANUP_PENDING", "ROLLBACK_CLEANUP_PENDING"}:
        for kind, expected in record.cleanup.items():
            directory = active if kind == "candidate" else backups.directory
            name = (record.restoration_name if record.restoration is not None else record.candidate_name
                    ) if kind == "candidate" else record.backup_name
            if _exists(directory, name):
                current, _ = named_file(directory, name)
                ctime_ns = (record.commit_ctimes[1] if record.commit_ctimes is not None
                            else None) if record.phase == "COMMIT_CLEANUP_PENDING" else (
                            record.rollback_ctimes[1] if record.rollback_ctimes is not None else None)
                if not matches(current, expected, exchanged=(kind == "candidate" and
                        record.phase != "PRECOMMIT_ABORT"),
                        ctime_ns=ctime_ns if kind == "candidate" else None):
                    raise FilesystemRefused("ARTIFACT_MISMATCH")


def authenticate_complete_cleanup(
    record: JournalRecord,
    authority: RecoveryAuthority,
    active: PinnedDirectory,
    backups: BackupStore,
) -> None:
    """Require both artifacts for the first durable cleanup decision.

    A published final decision uses authenticate_record instead: deletion may
    already have happened before a crash, so absence is then recoverable.
    """

    if record.phase not in {"PRECOMMIT_ABORT", "COMMIT_CLEANUP_PENDING", "ROLLBACK_CLEANUP_PENDING"}:
        raise FilesystemRefused("INVALID_TRANSITION")
    authenticate_record(record, authority, active, backups)
    backups.require(record.backup_name, record.backup)
    candidate_name = record.restoration_name if record.restoration is not None else record.candidate_name
    current, _ = named_file(active, candidate_name)
    expected = record.cleanup["candidate"]
    expected_ctime = (record.commit_ctimes[1] if record.phase == "COMMIT_CLEANUP_PENDING"
                      else record.rollback_ctimes[1] if record.phase == "ROLLBACK_CLEANUP_PENDING"
                      else None)
    if not (current.same_content_metadata(expected) if record.phase == "PRECOMMIT_ABORT"
            else current.same_after_exchange(expected) and current.ctime_ns == expected_ctime):
        raise FilesystemRefused("ARTIFACT_MISMATCH")
