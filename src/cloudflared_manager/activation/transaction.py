"""Internal, unwired config and service activation transaction."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cloudflared_manager.activation.authentication import (
    authenticate_complete_cleanup, authenticate_record,
)
from cloudflared_manager.activation.filesystem import (
    DirectoryFacts, FileFacts, FilesystemRefused, PinnedDirectory, _metadata_supported,
    exchange, file_facts, fsync_directory, named_file, require_source,
)
from cloudflared_manager.activation.journal import BaselineFacts, JournalRecord, JournalStore
from cloudflared_manager.activation.service import StrictService, ServiceRefused
from cloudflared_manager.activation.service_transaction import ServiceController, resume_service
from cloudflared_manager.activation.state import BackupStore, open_fixed_state_child, unlink_known
from cloudflared_manager.cloudflared.editing.candidate import CandidateCommitHandle
from cloudflared_manager.cloudflared.editing.preparation import (
    ConfigMutation, PreparationOutcome, prepare_validated_candidate,
)
from cloudflared_manager.cloudflared.editing.source import ConfigSourceSnapshot
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

    No CLI/web caller exists. Production owner and trust anchor are root and `/`.
    """

    def __init__(
        self,
        paths: DeploymentPaths,
        *,
        authority: AuthorityProvider | None = None,
        baseline: BaselineProvider | None = None,
        service: ServiceController | None = None,
        owner: int = 0,
        anchor: Path = Path("/"),
    ) -> None:
        self.paths = paths
        self.authority = authority or DeployedAuthority(paths)
        self.service = service or StrictService(self.authority)
        self.baseline = baseline or self.service
        self.owner = owner
        self.anchor = anchor
        if owner != 0 and anchor == Path("/"):
            raise FilesystemRefused("UNSAFE_TEST_BOUNDARY")

    def run(self, mutation: ConfigMutation, *, validator: CandidateValidator) -> FilesystemResult:
        if os.geteuid() != self.owner or self.baseline is None:
            raise ActivationError("PRIVILEGED_BOUNDARY_UNAVAILABLE")
        with DeploymentLock(self.paths.lock_path, owner=(self.owner, os.getegid())):
            with self._stores() as (journal, backups):
                journal.recover_staging(authenticate=lambda item: self._authenticate_record(item, backups))
                journal.require_clean()
                backups.require_empty()
                release, adopted = self.authority.current()
                with PinnedDirectory(adopted.parent, anchor=self.anchor, owner=self.owner) as active:
                    _metadata_supported(active.fd)
                    _require_no_orphan_candidates(active)
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
                    service_phase = False
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
                        # Observe the service first: that check may take time, so
                        # source, candidate, and authority must be checked after it.
                        if self._baseline(adopted, source) != baseline:
                            raise FilesystemRefused("BASELINE_CHANGED")
                        # No journal write or filesystem preparation intervenes
                        # between these final identity checks and exchange.
                        require_source(snapshot, active)
                        self._require_candidate(active, handle.name, candidate)
                        self._require_authority(release, adopted)
                        exchange(active, adopted.name, handle.name)
                        self._require_candidate(active, adopted.name, candidate, exchanged=True)
                        self._require_source_at(active, handle.name, source, exchanged=True)
                        os.fsync(handle.file_fd)
                        fsync_directory(active)
                        commit_ctimes = self._observed_ctimes(
                            active, adopted.name, handle.name, candidate, source,
                        )
                        record = journal.publish(record.successor("CONFIG_COMMITTED",
                                                                  commit_ctimes=commit_ctimes),
                                                 authenticate=lambda item: self._authenticate(item, active, backups))
                        service_phase = True
                        return FilesystemResult(self._resume_service(
                            record, journal, active, backups, adopted.name), transaction_id)
                    except Exception as error:
                        if service_phase:
                            # Never retry or select rollback after a failed service-
                            # phase publication in the same invocation. Recovery
                            # must establish durable authority afresh.
                            raise ActivationError("RECOVERY_REQUIRED", original=_failure_code(error)) from None
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
                            published = journal.recover_staging(
                                authenticate=lambda item: self._authenticate_record(item, backups)
                            )
                            if published is None:
                                self._cleanup_unpublished(
                                    active, backups, handle, backup_name, backup_facts,
                                    prepared.source,
                                )
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
                        pending = sys.exc_info()[1]
                        if handle is not None:
                            try:
                                handle.close()
                            except OSError:
                                if isinstance(pending, ActivationError):
                                    failures = [code for code in (pending.rollback, "DESCRIPTOR_CLOSE_FAILED") if code]
                                    raise ActivationError(
                                        "RECOVERY_REQUIRED", original=pending.original or pending.code,
                                        rollback=";".join(failures),
                                    ) from None
                                raise ActivationError(
                                    "RECOVERY_REQUIRED" if original else "DESCRIPTOR_CLOSE_FAILED",
                                    original=original,
                                    rollback="DESCRIPTOR_CLOSE_FAILED" if original else None,
                                ) from None
                        elif prepared.candidate is not None:
                            try:
                                prepared.discard()
                            except Exception:
                                if isinstance(pending, ActivationError):
                                    failures = [code for code in (pending.rollback, "CANDIDATE_CLEANUP_FAILED") if code]
                                    raise ActivationError(
                                        "RECOVERY_REQUIRED", original=pending.original or pending.code,
                                        rollback=";".join(failures),
                                    ) from None
                                raise ActivationError(
                                    "RECOVERY_REQUIRED" if original else "CANDIDATE_CLEANUP_FAILED",
                                    original=original,
                                    rollback="CANDIDATE_CLEANUP_FAILED" if original else None,
                                ) from None

    def recover(self) -> FilesystemResult:
        """Internal root recovery; service effects require durable journal authority."""

        if os.geteuid() != self.owner:
            raise ActivationError("PRIVILEGED_BOUNDARY_UNAVAILABLE")
        with DeploymentLock(self.paths.lock_path, owner=(self.owner, os.getegid())):
            with self._stores() as (journal, backups):
                try:
                    record = journal.recover_staging(
                        authenticate=lambda item: self._authenticate_record(item, backups)
                    )
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
                                }), authenticate=lambda item: self._authenticate(item, active, backups),
                                    authenticate_complete_cleanup=lambda item: self._authenticate_complete(
                                        item, active, backups))
                                self._finish_abort(abort, journal, active, backups)
                                return FilesystemResult("FAILED_PRECOMMIT", record.transaction_id)
                            if (record.phase == "CONFIG_COMMITTING"
                                and current.same_after_exchange(record.candidate)):
                                if not _leaf_exists(active, record.candidate_name):
                                    result = self._handle_failure(record, journal, active, backups, adopted.name)
                                    return FilesystemResult(result, record.transaction_id)
                                self._require_source_at(active, record.candidate_name, record.source, exchanged=True)
                                fd = os.open(adopted.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                             dir_fd=active.fd)
                                try:
                                    observed, _ = file_facts(fd)
                                    if not observed.same_after_exchange(record.candidate):
                                        raise FilesystemRefused("STALE_CANDIDATE")
                                    os.fsync(fd)
                                finally:
                                    os.close(fd)
                                fsync_directory(active)
                                self._authenticate(record, active, backups)
                                commit_ctimes = self._observed_ctimes(
                                    active, adopted.name, record.candidate_name,
                                    record.candidate, record.source,
                                )
                                committed = journal.publish(record.successor(
                                    "CONFIG_COMMITTED", commit_ctimes=commit_ctimes),
                                                            authenticate=lambda item: self._authenticate(item, active, backups))
                                return FilesystemResult(self._resume_service(
                                    committed, journal, active, backups, adopted.name, recovery=True), committed.transaction_id)
                            raise FilesystemRefused("ROLLBACK_FAILED_STATE_INDETERMINATE")
                        if record.phase == "PRECOMMIT_ABORT":
                            self._finish_abort(record, journal, active, backups)
                            return FilesystemResult("FAILED_PRECOMMIT", record.transaction_id)
                        if record.phase in {"ACTIVATION_FAILED", "ROLLBACK_CONFIG"}:
                            if record.phase == "ACTIVATION_FAILED" and not self.service.settled():
                                return FilesystemResult("RECOVERY_REQUIRED", record.transaction_id)
                            current, _ = named_file(active, adopted.name)
                            if current.same_after_exchange(record.restoration or record.source):
                                if record.phase == "ACTIVATION_FAILED":
                                    record = journal.publish(record.successor("ROLLBACK_CONFIG"),
                                                             authenticate=lambda item: self._authenticate(item, active, backups))
                                record = self._complete_rollback(record, journal, active, backups, adopted.name)
                                return FilesystemResult(self._resume_service(
                                    record, journal, active, backups, adopted.name, recovery=True), record.transaction_id)
                            result = self._handle_failure(record, journal, active, backups, adopted.name)
                            return FilesystemResult(result, record.transaction_id)
                        if record.phase in {"COMMIT_CLEANUP_PENDING", "ROLLBACK_CLEANUP_PENDING"}:
                            self._finish_cleanup(record, journal, active, backups)
                            return FilesystemResult(
                                "COMMITTED_SUCCESS" if record.phase == "COMMIT_CLEANUP_PENDING"
                                else "FAILED_ROLLED_BACK", record.transaction_id,
                            )
                        return FilesystemResult(self._resume_service(
                            record, journal, active, backups, adopted.name, recovery=True), record.transaction_id)
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
                published = journal.load()
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

    def _authenticate_record(self, record: JournalRecord, backups: BackupStore) -> None:
        release, adopted = self.authority.current()
        if release != record.release_id or _fingerprint(adopted) != record.adopted_fingerprint:
            raise FilesystemRefused("STALE_AUTHORITY")
        with PinnedDirectory(adopted.parent, anchor=self.anchor, owner=self.owner) as active:
            self._authenticate(record, active, backups)

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

    def _require_candidate(self, active: PinnedDirectory, name: str, expected: FileFacts,
                           *, exchanged: bool = False, ctime_ns: int | None = None) -> None:
        current, _ = named_file(active, name)
        if not (current.same_after_exchange(expected) if exchanged
                else current.same_content_metadata(expected)):
            raise FilesystemRefused("STALE_CANDIDATE")
        if ctime_ns is not None and current.ctime_ns != ctime_ns:
            raise FilesystemRefused("STALE_CANDIDATE")

    def _require_source_at(self, active: PinnedDirectory, name: str, expected: FileFacts,
                           *, exchanged: bool = False, ctime_ns: int | None = None) -> None:
        current, _ = named_file(active, name)
        if not (current.same_after_exchange(expected) if exchanged
                else current.same_content_metadata(expected)):
            raise FilesystemRefused("STALE_SOURCE")
        if ctime_ns is not None and current.ctime_ns != ctime_ns:
            raise FilesystemRefused("STALE_SOURCE")

    def _authenticate(self, record: JournalRecord, active: PinnedDirectory, backups: BackupStore) -> None:
        authenticate_record(record, self.authority, active, backups)

    def _authenticate_complete(self, record: JournalRecord, active: PinnedDirectory,
                               backups: BackupStore) -> None:
        authenticate_complete_cleanup(record, self.authority, active, backups)

    def _fsync_verified_active(self, active: PinnedDirectory, name: str, expected: FileFacts,
                               *, ctime_ns: int | None = None) -> None:
        active.revalidate()
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=active.fd)
        try:
            observed, _ = file_facts(fd)
            if not observed.same_after_exchange(expected):
                raise FilesystemRefused("ARTIFACT_MISMATCH")
            if ctime_ns is not None and observed.ctime_ns != ctime_ns:
                raise FilesystemRefused("ARTIFACT_MISMATCH")
            os.fsync(fd)
        finally:
            os.close(fd)
        self._require_source_at(active, name, expected, exchanged=True, ctime_ns=ctime_ns)
        fsync_directory(active)

    def _observed_ctimes(self, active: PinnedDirectory, active_name: str, other_name: str,
                         active_expected: FileFacts, other_expected: FileFacts) -> tuple[int, int]:
        active_facts, _ = named_file(active, active_name)
        other_facts, _ = named_file(active, other_name)
        if (not active_facts.same_after_exchange(active_expected)
            or not other_facts.same_after_exchange(other_expected)):
            raise FilesystemRefused("ARTIFACT_MISMATCH")
        return active_facts.ctime_ns, other_facts.ctime_ns

    def _complete_rollback(self, record: JournalRecord, journal: JournalStore,
                           active: PinnedDirectory, backups: BackupStore,
                           active_name: str) -> JournalRecord:
        expected = record.restoration or record.source
        self._fsync_verified_active(active, active_name, expected,
                                    ctime_ns=record.rollback_ctimes[0] if record.rollback_ctimes else None)
        if record.rollback_ctimes is None:
            other_name = record.restoration_name if record.restoration is not None else record.candidate_name
            ctimes = self._observed_ctimes(active, active_name, other_name,
                                           expected, record.candidate)
            record = journal.publish(record.successor("ROLLBACK_CONFIG", rollback_ctimes=ctimes),
                            authenticate=lambda item: self._authenticate(item, active, backups))
        else:
            self._authenticate(record, active, backups)
        return record

    def _resume_service(self, record, journal, active, backups, active_name, *, recovery=False):
        return resume_service(self, record, journal, active, backups, active_name, recovery=recovery)

    def _cleanup_unpublished(self, active: PinnedDirectory, backups: BackupStore,
                             handle: CandidateCommitHandle | None, backup_name: str | None,
                             backup_facts: FileFacts | None, snapshot: ConfigSourceSnapshot) -> None:
        if handle is not None:
            facts, _ = named_file(active, handle.name)
            if ((facts.device, facts.inode, facts.size, facts.sha256)
                != (handle.device, handle.inode, handle.size, handle.sha256)
                or (facts.uid, facts.gid, facts.mode) not in {
                    (self.owner, os.getegid(), 0o600),
                    (snapshot.uid, snapshot.gid, 0o600),
                    (snapshot.uid, snapshot.gid, snapshot.permission_mode),
                }):
                raise FilesystemRefused("ARTIFACT_MISMATCH")
            unlink_known(active, handle.name, facts)
        if backup_name is not None and backup_facts is not None:
            unlink_known(backups.directory, backup_name, backup_facts)

    def _handle_failure(self, record: JournalRecord, journal: JournalStore, active: PinnedDirectory,
                        backups: BackupStore, active_name: str) -> str:
        current, _ = named_file(active, active_name)
        if current.same_content_metadata(record.source) and record.phase in {"BACKUP_DURABLE", "CONFIG_COMMITTING"}:
            abort = record.successor("PRECOMMIT_ABORT", cleanup={"candidate": record.candidate, "backup": record.backup})
            abort = journal.publish(abort,
                                    authenticate=lambda item: self._authenticate(item, active, backups),
                                    authenticate_complete_cleanup=lambda item: self._authenticate_complete(
                                        item, active, backups))
            self._finish_abort(abort, journal, active, backups)
            return "FAILED_PRECOMMIT"
        if record.phase == "ROLLBACK_CONFIG" and current.same_after_exchange(record.restoration or record.source):
            self._authenticate(record, active, backups)
            record = self._complete_rollback(record, journal, active, backups, active_name)
            return self._resume_service(record, journal, active, backups, active_name)
        if (not current.same_after_exchange(record.candidate)
            or (record.commit_ctimes is not None and current.ctime_ns != record.commit_ctimes[0])):
            raise FilesystemRefused("ROLLBACK_FAILED_STATE_INDETERMINATE")
        if record.phase not in {"CONFIG_COMMITTING", "CONFIG_COMMITTED", "SERVICE_ACTIVATING", "ACTIVATION_FAILED", "ROLLBACK_CONFIG"}:
            raise FilesystemRefused("RECOVERY_REQUIRED")
        if not self.service.settled():
            raise FilesystemRefused("RECOVERY_REQUIRED")
        displaced_exists = _leaf_exists(active, record.candidate_name)
        if displaced_exists:
            self._require_source_at(active, record.candidate_name, record.source, exchanged=True,
                                    ctime_ns=record.commit_ctimes[1] if record.commit_ctimes else None)
        elif record.restoration is None and _leaf_exists(active, record.restoration_name):
            # A pre-publication restoration file has no journaled inode. Keep
            # it and the rollback intent for explicit manual review.
            raise FilesystemRefused("UNJOURNALED_RESTORATION")
        if record.restoration is not None:
            if displaced_exists:
                raise FilesystemRefused("ARTIFACT_MISMATCH")
            self._require_source_at(active, record.restoration_name, record.restoration)
        if record.phase in {"CONFIG_COMMITTING", "CONFIG_COMMITTED", "SERVICE_ACTIVATING"}:
            record = journal.publish(record.successor("ACTIVATION_FAILED"), authenticate=lambda item: self._authenticate(item, active, backups))
        if record.phase == "ACTIVATION_FAILED":
            record = journal.publish(record.successor("ROLLBACK_CONFIG"), authenticate=lambda item: self._authenticate(item, active, backups))
        if not displaced_exists and record.restoration is None:
            restored = self._stage_backup_restoration(record, active, backups)
            record = journal.publish(record.successor("ROLLBACK_CONFIG", restoration=restored),
                                     authenticate=lambda item: self._authenticate(item, active, backups))
        self._authenticate(record, active, backups)
        target_name = record.restoration_name if record.restoration is not None else record.candidate_name
        self._require_candidate(active, active_name, record.candidate, exchanged=True,
                                ctime_ns=record.commit_ctimes[0] if record.commit_ctimes else None)
        self._require_source_at(active, target_name, record.restoration or record.source,
                                exchanged=record.restoration is None,
                                ctime_ns=(record.commit_ctimes[1] if record.restoration is None
                                          and record.commit_ctimes else None))
        exchange(active, active_name, target_name)
        self._require_source_at(active, active_name, record.restoration or record.source, exchanged=True)
        self._require_candidate(active, target_name, record.candidate, exchanged=True)
        record = self._complete_rollback(record, journal, active, backups, active_name)
        return self._resume_service(record, journal, active, backups, active_name)

    def _stage_backup_restoration(self, record: JournalRecord, active: PinnedDirectory,
                                  backups: BackupStore) -> FileFacts:
        backups.require(record.backup_name, record.backup)
        backup_facts, data = named_file(backups.directory, record.backup_name)
        if (not backup_facts.same_content_metadata(record.backup)
            or len(data) != record.source.size
            or hashlib.sha256(data).hexdigest() != record.source.sha256):
            raise FilesystemRefused("BACKUP_MISMATCH")
        active.revalidate()
        fd: int | None = None
        try:
            fd = os.open(record.restoration_name,
                         os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=active.fd)
            initial, _ = file_facts(fd)
            if (initial.uid != self.owner or initial.mode != 0o600
                or initial.device != record.parent.device or initial.links != 1):
                raise FilesystemRefused("UNSAFE_RESTORATION")
            remaining = memoryview(data)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0 or written > len(remaining):
                    raise FilesystemRefused("RESTORATION_WRITE_FAILED")
                remaining = remaining[written:]
            if (initial.uid, initial.gid) != (record.source.uid, record.source.gid):
                os.fchown(fd, record.source.uid, record.source.gid)
            os.fchmod(fd, record.source.mode)
            os.fsync(fd)
            staged, copied = file_facts(fd)
            if (copied != data or staged.inode == record.candidate.inode
                or (staged.device, staged.uid, staged.gid, staged.mode, staged.size, staged.sha256)
                != (record.parent.device, record.source.uid, record.source.gid,
                    record.source.mode, record.source.size, record.source.sha256)):
                raise FilesystemRefused("RESTORATION_MISMATCH")
        except FilesystemRefused:
            raise
        except OSError:
            raise FilesystemRefused("RESTORATION_STAGE_FAILED") from None
        finally:
            if fd is not None:
                os.close(fd)
        fsync_directory(active)
        visible, copied = named_file(active, record.restoration_name)
        if not visible.same_content_metadata(staged) or copied != data:
            raise FilesystemRefused("RESTORATION_MISMATCH")
        return visible

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
        candidate_name = record.restoration_name if record.restoration is not None else record.candidate_name
        ctimes = (record.commit_ctimes if record.phase == "COMMIT_CLEANUP_PENDING"
                  else record.rollback_ctimes)
        assert ctimes is not None
        unlink_known(active, candidate_name, record.cleanup["candidate"],
                     missing_ok=True, exchanged=True, ctime_ns=ctimes[1])
        unlink_known(backups.directory, record.backup_name, record.cleanup["backup"], missing_ok=True)
        fsync_directory(active)
        fsync_directory(backups.directory)
        journal.retire(record, authenticate=lambda item: self._authenticate(item, active, backups))


def _fingerprint(path: Path) -> str:
    return hashlib.sha256(os.fsencode(path)).hexdigest()


_CANDIDATE_LEAF = re.compile(r"^\.cfm-candidate-[0-9a-f]{32}\.yaml$")
_RESTORATION_LEAF = re.compile(r"^\.cfm-restore-[0-9a-f]{32}\.yaml$")


def _leaf_exists(directory: PinnedDirectory, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _require_no_orphan_candidates(active: PinnedDirectory) -> None:
    active.revalidate()
    try:
        with os.scandir(active.fd) as entries:
            for count, entry in enumerate(entries, start=1):
                if count > 4096 or _CANDIDATE_LEAF.fullmatch(entry.name) or _RESTORATION_LEAF.fullmatch(entry.name):
                    raise FilesystemRefused("ORPHAN_CANDIDATE_REQUIRES_REVIEW")
    except OSError:
        raise FilesystemRefused("UNSAFE_DIRECTORY") from None
    active.revalidate()


def _failure_code(error: Exception) -> str:
    if isinstance(error, ServiceRefused):
        return "SERVICE_BASELINE_UNAVAILABLE"
    if isinstance(error, FilesystemRefused):
        return error.code
    if isinstance(error, ActivationError):
        return error.code
    return "FILESYSTEM_TRANSACTION_FAILED"
