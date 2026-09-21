"""Unwired filesystem transaction prototype; production activation needs PR B."""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cloudflared_manager.activation.filesystem import (
    DirectoryFacts, FileFacts, FilesystemRefused, PinnedDirectory, _metadata_supported,
    exchange, file_facts, fsync_directory, named_file, require_source,
)
from cloudflared_manager.activation.journal import BaselineFacts, JournalRecord, JournalStore
from cloudflared_manager.activation.state import BackupStore, open_fixed_state_child, unlink_known
from cloudflared_manager.cloudflared.editing.candidate import CandidateCommitHandle
from cloudflared_manager.cloudflared.editing.preparation import (
    ConfigMutation, PreparationOutcome, prepare_validated_candidate,
)
from cloudflared_manager.cloudflared.editing.validation import CandidateValidator
from cloudflared_manager.deployment.environment import read_environment, require_safe_environment
from cloudflared_manager.deployment.paths import DeploymentPaths
from cloudflared_manager.deployment.release import DeploymentLock, ReleaseFilesystem
from cloudflared_manager.deployment.settings import settings_from_document
from cloudflared_manager.runtime_identity import PROCESS_RELEASE_ID


class AuthorityProvider(Protocol):
    def current(self) -> tuple[str, Path]:
        """Reread immutable release and root-owned explicitly adopted path."""


class BaselineProvider(Protocol):
    def observe(self, *, adopted_fingerprint: str, source_digest: str) -> BaselineFacts:
        """Return a complete healthy service observation or fail closed."""


@dataclass(frozen=True, slots=True)
class DeployedAuthority:
    paths: DeploymentPaths

    def current(self) -> tuple[str, Path]:
        require_safe_environment(self.paths.environment_file, owner=(0, 0))
        settings = settings_from_document(read_environment(self.paths.environment_file)[0])
        if settings.cloudflared_config_path is None:
            raise FilesystemRefused("NOT_ADOPTED")
        release = ReleaseFilesystem(self.paths).read_current_sha()
        if PROCESS_RELEASE_ID is None or release != PROCESS_RELEASE_ID:
            raise FilesystemRefused("STALE_RELEASE")
        return release, settings.cloudflared_config_path


class ActivationError(Exception):
    """Sanitized result preserving both original failure and rollback outcome."""

    def __init__(self, code: str, *, original: str | None = None, rollback: str | None = None) -> None:
        self.code = code
        self.original = original
        self.rollback = rollback
        super().__init__(code)

    def __repr__(self) -> str:
        return f"ActivationError(code={self.code!r}, original={self.original!r}, rollback={self.rollback!r})"


@dataclass(frozen=True, slots=True)
class FilesystemResult:
    code: str
    transaction_id: str | None = None


class FilesystemActivation:
    """Internal root transaction with injectable test authority and baseline.

    No production baseline provider or CLI/web caller exists in PR A. In
    production, the owner and trust anchor are fixed to root and `/`.
    """

    def __init__(
        self,
        paths: DeploymentPaths,
        *,
        authority: AuthorityProvider | None = None,
        baseline: BaselineProvider | None = None,
        owner: int = 0,
        anchor: Path = Path("/"),
    ) -> None:
        self.paths = paths
        self.authority = authority or DeployedAuthority(paths)
        self.baseline = baseline
        self.owner = owner
        self.anchor = anchor
        if owner != 0 and anchor == Path("/"):
            raise FilesystemRefused("UNSAFE_TEST_BOUNDARY")

    def run(self, mutation: ConfigMutation, *, validator: CandidateValidator) -> FilesystemResult:
        if os.geteuid() != self.owner or self.baseline is None:
            raise ActivationError("PRIVILEGED_BOUNDARY_UNAVAILABLE")
        with DeploymentLock(self.paths.lock_path, owner=(self.owner, os.getegid())):
            with self._stores() as (journal, backups):
                journal.recover_staging()
                journal.require_clean()
                release, adopted = self.authority.current()
                with PinnedDirectory(adopted.parent, anchor=self.anchor, owner=self.owner) as active:
                    _metadata_supported(active.fd)
                    prepared = prepare_validated_candidate(adopted, mutation, cloudflared_validator=validator)
                    if prepared.outcome is PreparationOutcome.NO_CHANGE:
                        require_source(prepared.source, active)
                        return FilesystemResult("NO_CHANGE")
                    if prepared.candidate is None:
                        raise ActivationError("VALIDATION_FAILED")
                    handle: CandidateCommitHandle | None = None
                    backup_name: str | None = None
                    backup_facts: FileFacts | None = None
                    record: JournalRecord | None = None
                    original: str | None = None
                    transaction_id = secrets.token_hex(16)
                    try:
                        snapshot = prepared.source
                        source = require_source(snapshot, active)
                        self._require_authority(release, adopted)
                        self._baseline(adopted, source)
                        handle = prepared.candidate.consume_for_activation()
                        if DirectoryFacts.from_stat(os.fstat(handle.directory_fd)) != active.facts:
                            raise FilesystemRefused("STALE_CANDIDATE")
                        candidate = self._convert_candidate(handle, source, active)
                        source = require_source(snapshot, active)
                        self._require_authority(release, adopted)
                        baseline = self._baseline(adopted, source)
                        backup_name = f"backup-{transaction_id}"
                        backup_facts = backups.create(backup_name, snapshot.original_bytes, source)
                        backups.require(backup_name, backup_facts)
                        record = JournalRecord(
                            1, transaction_id, 1, None, None, None, "BACKUP_DURABLE", release,
                            _fingerprint(adopted), source, candidate, backup_facts,
                            active.facts, handle.name, backup_name, baseline, {},
                        )
                        journal.publish(record, authenticate=lambda item: self._authenticate(item, active, backups))
                        record = journal.publish(record.successor("CONFIG_COMMITTING"),
                                                 authenticate=lambda item: self._authenticate(item, active, backups))
                        # This is the final observation. There is no journal write or
                        # filesystem preparation between it and the exchange.
                        require_source(snapshot, active)
                        self._require_candidate(active, handle.name, candidate)
                        self._require_authority(release, adopted)
                        if self._baseline(adopted, source) != baseline:
                            raise FilesystemRefused("BASELINE_CHANGED")
                        exchange(active, adopted.name, handle.name)
                        self._require_candidate(active, adopted.name, candidate)
                        self._require_source_at(active, handle.name, source)
                        os.fsync(handle.file_fd)
                        fsync_directory(active)
                        record = journal.publish(record.successor("CONFIG_COMMITTED"),
                                                 authenticate=lambda item: self._authenticate(item, active, backups))
                        # PR B must resume service activation or select rollback.
                        return FilesystemResult("SERVICE_ACTIVATION_PENDING", transaction_id)
                    except Exception as error:
                        original = (
                            error.original_code
                            if isinstance(error, FilesystemRefused) and error.original_code is not None
                            else _failure_code(error)
                        )
                        initial_cleanup_failure = (
                            error.code
                            if isinstance(error, FilesystemRefused) and error.original_code is not None
                            else None
                        )
                        try:
                            published = journal.recover_staging()
                            if published is None:
                                self._cleanup_unpublished(active, backups, handle, backup_name, backup_facts)
                                journal.require_clean()
                                raise ActivationError(initial_cleanup_failure or original,
                                                      original=original if initial_cleanup_failure else None,
                                                      rollback=initial_cleanup_failure) from None
                            outcome = self._handle_failure(published, journal, active, backups, adopted.name)
                            raise ActivationError(outcome, original=original) from None
                        except ActivationError:
                            raise
                        except Exception as recovery_error:
                            raise ActivationError("RECOVERY_REQUIRED", original=original,
                                                  rollback=_failure_code(recovery_error)) from None
                    finally:
                        if handle is not None:
                            try:
                                handle.close()
                            except OSError:
                                if original is None:
                                    raise ActivationError("DESCRIPTOR_CLOSE_FAILED") from None
                        elif prepared.candidate is not None:
                            try:
                                prepared.discard()
                            except Exception:
                                if original is None:
                                    raise ActivationError("CANDIDATE_CLEANUP_FAILED") from None

    def recover(self) -> FilesystemResult:
        """Root-only explicit recovery of filesystem phases; never controls service."""

        if os.geteuid() != self.owner:
            raise ActivationError("PRIVILEGED_BOUNDARY_UNAVAILABLE")
        with DeploymentLock(self.paths.lock_path, owner=(self.owner, os.getegid())):
            with self._stores() as (journal, backups):
                try:
                    record = journal.recover_staging()
                    if record is None:
                        journal.require_clean()
                        return FilesystemResult("NO_RECOVERY_REQUIRED")
                    release, adopted = self.authority.current()
                    if release != record.release_id or _fingerprint(adopted) != record.adopted_fingerprint:
                        raise FilesystemRefused("STALE_AUTHORITY")
                    with PinnedDirectory(adopted.parent, anchor=self.anchor, owner=self.owner) as active:
                        self._authenticate(record, active, backups)
                        if record.phase in {"BACKUP_DURABLE", "CONFIG_COMMITTING"}:
                            current, _ = named_file(active, adopted.name)
                            if current.same_content_metadata(record.source):
                                abort = journal.publish(record.successor("PRECOMMIT_ABORT", cleanup={
                                    "candidate": record.candidate, "backup": record.backup,
                                }), authenticate=lambda item: self._authenticate(item, active, backups))
                                self._finish_abort(abort, journal, active, backups)
                                return FilesystemResult("FAILED_PRECOMMIT", record.transaction_id)
                            if (record.phase == "CONFIG_COMMITTING"
                                and current.same_content_metadata(record.candidate)):
                                self._require_source_at(active, record.candidate_name, record.source)
                                committed = journal.publish(record.successor("CONFIG_COMMITTED"),
                                                            authenticate=lambda item: self._authenticate(item, active, backups))
                                return FilesystemResult("SERVICE_ACTIVATION_PENDING", committed.transaction_id)
                            raise FilesystemRefused("ROLLBACK_FAILED_STATE_INDETERMINATE")
                        if record.phase == "PRECOMMIT_ABORT":
                            self._finish_abort(record, journal, active, backups)
                            return FilesystemResult("FAILED_PRECOMMIT", record.transaction_id)
                        if record.phase in {"ACTIVATION_FAILED", "ROLLBACK_CONFIG"}:
                            current, _ = named_file(active, adopted.name)
                            if current.same_content_metadata(record.source):
                                fsync_directory(active)
                                return FilesystemResult("CONFIG_RESTORED_SERVICE_PENDING", record.transaction_id)
                            result = self._handle_failure(record, journal, active, backups, adopted.name)
                            return FilesystemResult(result, record.transaction_id)
                        if record.phase in {"COMMIT_CLEANUP_PENDING", "ROLLBACK_CLEANUP_PENDING"}:
                            self._finish_cleanup(record, journal, active, backups)
                            return FilesystemResult(
                                "COMMITTED_SUCCESS" if record.phase == "COMMIT_CLEANUP_PENDING"
                                else "FAILED_ROLLED_BACK", record.transaction_id,
                            )
                        return FilesystemResult("SERVICE_ACTIVATION_PENDING", record.transaction_id)
                except ActivationError:
                    raise
                except Exception as error:
                    raise ActivationError("RECOVERY_REQUIRED", original=_failure_code(error)) from None

    def _stores(self):
        from contextlib import contextmanager

        @contextmanager
        def opened():
            journal_dir = open_fixed_state_child(self.paths.config_root, "activation-journal", anchor=self.anchor, owner=self.owner)
            try:
                journal = JournalStore(journal_dir, owner=self.owner)
                published = journal.recover_staging()
                backup_dir = open_fixed_state_child(
                    self.paths.config_root, "activation-backups", anchor=self.anchor,
                    owner=self.owner, create=published is None,
                )
                try:
                    yield journal, BackupStore(backup_dir, owner=self.owner)
                finally:
                    backup_dir.close()
            finally:
                journal_dir.close()
        return opened()

    def _require_authority(self, release: str, adopted: Path) -> None:
        if self.authority.current() != (release, adopted):
            raise FilesystemRefused("STALE_AUTHORITY")

    def _baseline(self, adopted: Path, source: FileFacts) -> BaselineFacts:
        assert self.baseline is not None
        fingerprint = _fingerprint(adopted)
        result = self.baseline.observe(adopted_fingerprint=fingerprint, source_digest=source.sha256)
        result = BaselineFacts.parse(result.record())
        if result.adopted_fingerprint != fingerprint or result.source_digest != source.sha256:
            raise FilesystemRefused("BASELINE_CHANGED")
        return result

    def _convert_candidate(self, handle: CandidateCommitHandle, source: FileFacts, active: PinnedDirectory) -> FileFacts:
        try:
            before, _ = file_facts(handle.file_fd)
            if ((before.device, before.inode, before.size, before.sha256) !=
                (handle.device, handle.inode, handle.size, handle.sha256)
                or before.mode != 0o600):
                raise FilesystemRefused("STALE_CANDIDATE")
            if (before.uid, before.gid) != (source.uid, source.gid):
                os.fchown(handle.file_fd, source.uid, source.gid)
            os.fchmod(handle.file_fd, source.mode)
            os.fsync(handle.file_fd)
            fsync_directory(active)
            after, _ = file_facts(handle.file_fd)
            visible, _ = named_file(active, handle.name)
            if (not after.same_content_metadata(visible)
                or (after.device, after.inode, after.size, after.sha256, after.uid, after.gid, after.mode)
                != (handle.device, handle.inode, handle.size, handle.sha256, source.uid, source.gid, source.mode)):
                raise FilesystemRefused("STALE_CANDIDATE")
            return after
        except OSError:
            raise FilesystemRefused("CANDIDATE_CONVERSION_FAILED") from None

    def _require_candidate(self, active: PinnedDirectory, name: str, expected: FileFacts) -> None:
        current, _ = named_file(active, name)
        if not current.same_content_metadata(expected):
            raise FilesystemRefused("STALE_CANDIDATE")

    def _require_source_at(self, active: PinnedDirectory, name: str, expected: FileFacts) -> None:
        current, _ = named_file(active, name)
        if not current.same_content_metadata(expected):
            raise FilesystemRefused("STALE_SOURCE")

    def _authenticate(self, record: JournalRecord, active: PinnedDirectory, backups: BackupStore) -> None:
        release, adopted = self.authority.current()
        if (release != record.release_id or adopted.parent != active.path
            or _fingerprint(adopted) != record.adopted_fingerprint
            or active.facts != record.parent):
            raise FilesystemRefused("STALE_AUTHORITY")
        active.revalidate()
        _metadata_supported(active.fd)
        if record.phase not in {"PRECOMMIT_ABORT", "COMMIT_CLEANUP_PENDING", "ROLLBACK_CLEANUP_PENDING"}:
            backups.require(record.backup_name, record.backup)
        if record.phase in {"BACKUP_DURABLE", "PRECOMMIT_ABORT"}:
            self._require_source_at(active, adopted.name, record.source)
            if record.phase != "PRECOMMIT_ABORT" or _exists(active, record.candidate_name):
                self._require_candidate(active, record.candidate_name, record.candidate)
        elif record.phase == "CONFIG_COMMITTING":
            current, _ = named_file(active, adopted.name)
            if current.same_content_metadata(record.source):
                self._require_candidate(active, record.candidate_name, record.candidate)
            elif not current.same_content_metadata(record.candidate):
                raise FilesystemRefused("UNKNOWN_ACTIVE_STATE")
        elif record.phase in {"CONFIG_COMMITTED", "ACTIVATION_FAILED", "ROLLBACK_CONFIG", "SERVICE_ACTIVATING", "SERVICE_VERIFIED", "COMMIT_CLEANUP_PENDING"}:
            current, _ = named_file(active, adopted.name)
            if record.phase == "ROLLBACK_CONFIG" and current.same_content_metadata(record.source):
                self._require_candidate(active, record.candidate_name, record.candidate)
            elif not current.same_content_metadata(record.candidate):
                raise FilesystemRefused("UNKNOWN_ACTIVE_STATE")
            if record.phase in {"CONFIG_COMMITTED", "SERVICE_ACTIVATING", "SERVICE_VERIFIED"}:
                self._require_source_at(active, record.candidate_name, record.source)
        elif record.phase == "ROLLBACK_CLEANUP_PENDING":
            self._require_source_at(active, adopted.name, record.source)
        if record.phase in {"PRECOMMIT_ABORT", "COMMIT_CLEANUP_PENDING", "ROLLBACK_CLEANUP_PENDING"}:
            for kind, expected in record.cleanup.items():
                directory = active if kind == "candidate" else backups.directory
                name = record.candidate_name if kind == "candidate" else record.backup_name
                if _exists(directory, name):
                    current, _ = named_file(directory, name)
                    if not current.same_content_metadata(expected):
                        raise FilesystemRefused("ARTIFACT_MISMATCH")

    def _cleanup_unpublished(self, active: PinnedDirectory, backups: BackupStore,
                             handle: CandidateCommitHandle | None, backup_name: str | None,
                             backup_facts: FileFacts | None) -> None:
        if handle is not None:
            facts, _ = named_file(active, handle.name)
            if (facts.device, facts.inode) != (handle.device, handle.inode):
                raise FilesystemRefused("ARTIFACT_MISMATCH")
            unlink_known(active, handle.name, facts)
        if backup_name is not None and backup_facts is not None:
            unlink_known(backups.directory, backup_name, backup_facts)

    def _handle_failure(self, record: JournalRecord, journal: JournalStore, active: PinnedDirectory,
                        backups: BackupStore, active_name: str) -> str:
        current, _ = named_file(active, active_name)
        if current.same_content_metadata(record.source) and record.phase in {"BACKUP_DURABLE", "CONFIG_COMMITTING"}:
            abort = record.successor("PRECOMMIT_ABORT", cleanup={"candidate": record.candidate, "backup": record.backup})
            abort = journal.publish(abort, authenticate=lambda item: self._authenticate(item, active, backups))
            self._finish_abort(abort, journal, active, backups)
            return "FAILED_PRECOMMIT"
        if not current.same_content_metadata(record.candidate):
            raise FilesystemRefused("ROLLBACK_FAILED_STATE_INDETERMINATE")
        if record.phase not in {"CONFIG_COMMITTING", "CONFIG_COMMITTED", "ACTIVATION_FAILED", "ROLLBACK_CONFIG"}:
            raise FilesystemRefused("RECOVERY_REQUIRED")
        self._require_source_at(active, record.candidate_name, record.source)
        if record.phase in {"CONFIG_COMMITTING", "CONFIG_COMMITTED"}:
            record = journal.publish(record.successor("ACTIVATION_FAILED"), authenticate=lambda item: self._authenticate(item, active, backups))
        if record.phase == "ACTIVATION_FAILED":
            record = journal.publish(record.successor("ROLLBACK_CONFIG"), authenticate=lambda item: self._authenticate(item, active, backups))
        exchange(active, active_name, record.candidate_name)
        self._require_source_at(active, active_name, record.source)
        self._require_candidate(active, record.candidate_name, record.candidate)
        fd = os.open(active_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=active.fd)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        fsync_directory(active)
        return "CONFIG_RESTORED_SERVICE_PENDING"

    def _finish_abort(self, record: JournalRecord, journal: JournalStore,
                      active: PinnedDirectory, backups: BackupStore) -> None:
        self._authenticate(record, active, backups)
        unlink_known(active, record.candidate_name, record.candidate, missing_ok=True)
        unlink_known(backups.directory, record.backup_name, record.backup, missing_ok=True)
        fsync_directory(active)
        fsync_directory(backups.directory)
        journal.retire(record, authenticate=lambda item: self._authenticate(item, active, backups))

    def _finish_cleanup(self, record: JournalRecord, journal: JournalStore,
                        active: PinnedDirectory, backups: BackupStore) -> None:
        self._authenticate(record, active, backups)
        unlink_known(active, record.candidate_name, record.cleanup["candidate"], missing_ok=True)
        unlink_known(backups.directory, record.backup_name, record.cleanup["backup"], missing_ok=True)
        fsync_directory(active)
        fsync_directory(backups.directory)
        journal.retire(record, authenticate=lambda item: self._authenticate(item, active, backups))


def _fingerprint(path: Path) -> str:
    return hashlib.sha256(os.fsencode(path)).hexdigest()


def _exists(directory: PinnedDirectory, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _failure_code(error: Exception) -> str:
    if isinstance(error, FilesystemRefused):
        return error.code
    if isinstance(error, ActivationError):
        return error.code
    return "FILESYSTEM_TRANSACTION_FAILED"
