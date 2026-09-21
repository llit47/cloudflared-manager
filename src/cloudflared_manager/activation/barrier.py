"""Persistent activation-recovery gate for root manager authority mutations."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from cloudflared_manager.activation.filesystem import (
    FilesystemRefused, PinnedDirectory, fsync_directory, named_file,
)
from cloudflared_manager.activation.journal import JournalRecord, JournalStore
from cloudflared_manager.activation.state import BackupStore, open_fixed_state_child
from cloudflared_manager.activation.authentication import authenticate_record
from cloudflared_manager.deployment.environment import read_environment, require_safe_environment
from cloudflared_manager.deployment.errors import DeploymentError, HostOperationError
from cloudflared_manager.deployment.paths import DeploymentPaths
from cloudflared_manager.deployment.release import ReleaseFilesystem
from cloudflared_manager.deployment.settings import settings_from_document


class ActivationBarrierError(HostOperationError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("Activation recovery must be completed before manager configuration changes.")


class ActivationRecoveryBarrier:
    """Call only while holding the shared outer DeploymentLock."""

    def __init__(self, paths: DeploymentPaths, *, anchor: Path = Path("/"), owner: int = 0) -> None:
        self.paths = paths
        self.anchor = anchor
        self.owner = owner

    def require_clean(self) -> None:
        try:
            with open_fixed_state_child(self.paths.config_root, "activation-journal",
                                        anchor=self.anchor, owner=self.owner) as journal_dir:
                journal = JournalStore(journal_dir, owner=self.owner)
                published = journal.load()
                if published is None:
                    journal.recover_staging()
                    journal.require_clean()
                    return
                with open_fixed_state_child(self.paths.config_root, "activation-backups",
                                            anchor=self.anchor, owner=self.owner,
                                            create=False) as backup_dir:
                    backups = BackupStore(backup_dir, owner=self.owner)
                    _, adopted = self._authority()
                    with PinnedDirectory(adopted.parent, anchor=self.anchor, owner=self.owner) as active:
                        authenticate = lambda item: authenticate_record(item, self, active, backups)
                        record = journal.recover_staging(authenticate=authenticate)
                        if record is None:
                            raise FilesystemRefused("UNSAFE_JOURNAL")
                        # This gate retires a final decision only when all
                        # allowlisted artifacts are already absent.
                        if record.phase not in {"PRECOMMIT_ABORT", "COMMIT_CLEANUP_PENDING", "ROLLBACK_CLEANUP_PENDING"}:
                            raise FilesystemRefused("RECOVERY_REQUIRED")
                        self._authenticate_absent_cleanup(record, active, backup_dir, adopted.name)
                        fsync_directory(active)
                        fsync_directory(backup_dir)
                        journal.retire(record, authenticate=lambda item: self._authenticate_absent_cleanup(
                            item, active, backup_dir, adopted.name))
                journal.require_clean()
        except (FilesystemRefused, DeploymentError, OSError) as error:
            code = error.code if isinstance(error, FilesystemRefused) else "JOURNAL_NAMESPACE_NOT_DURABLY_CLEAN"
            raise ActivationBarrierError(code) from None

    def _authority(self) -> tuple[str, Path]:
        require_safe_environment(self.paths.environment_file, owner=(self.owner, os.getegid()))
        settings = settings_from_document(read_environment(self.paths.environment_file)[0])
        if settings.cloudflared_config_path is None:
            raise FilesystemRefused("STALE_AUTHORITY")
        release = ReleaseFilesystem(self.paths, owner=(self.owner, os.getegid())).read_current_sha()
        return release, settings.cloudflared_config_path

    def current(self) -> tuple[str, Path]:
        return self._authority()

    def _authenticate_absent_cleanup(self, record: JournalRecord, active: PinnedDirectory,
                                     backup_dir: PinnedDirectory, active_name: str) -> None:
        release, adopted = self._authority()
        if (record.phase not in {"PRECOMMIT_ABORT", "COMMIT_CLEANUP_PENDING", "ROLLBACK_CLEANUP_PENDING"}
            or release != record.release_id or adopted != active.path / active_name
            or hashlib.sha256(os.fsencode(adopted)).hexdigest() != record.adopted_fingerprint
            or active.facts != record.parent):
            raise FilesystemRefused("STALE_AUTHORITY")
        expected = record.candidate if record.phase == "COMMIT_CLEANUP_PENDING" else record.source
        current, _ = named_file(active, active_name)
        if not current.same_content_metadata(expected):
            raise FilesystemRefused("UNKNOWN_ACTIVE_STATE")
        for directory, name in ((active, record.candidate_name), (backup_dir, record.backup_name)):
            try:
                os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise FilesystemRefused("RECOVERY_REQUIRED")
