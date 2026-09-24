"""Exact-release update transaction with unit and health rollback."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cloudflared_manager.deployment.environment import read_environment
from cloudflared_manager.deployment.errors import (
    HostOperationError,
    RollbackError,
    TransactionFailedError,
)
from cloudflared_manager.deployment.health import verify_managed_health
from cloudflared_manager.deployment.paths import DeploymentPaths
from cloudflared_manager.deployment.protocols import HealthVerifier, ManagerService
from cloudflared_manager.deployment.reconciliation import DeploymentReconciler
from cloudflared_manager.deployment.release import ReleaseFilesystem
from cloudflared_manager.deployment.settings import settings_from_document
from cloudflared_manager.deployment.validation import validate_sha


@dataclass(frozen=True, slots=True)
class UpdateResult:
    sha: str
    changed: bool


class Updater:
    """Activate one exact candidate and restore the previous healthy release."""

    def __init__(
        self,
        paths: DeploymentPaths,
        filesystem: ReleaseFilesystem,
        service: ManagerService,
        health: HealthVerifier,
    ) -> None:
        self.paths = paths
        self.filesystem = filesystem
        self.service = service
        self.health = health

    def update(self, source: Path | None, sha: str, python: Path) -> UpdateResult:
        revision = validate_sha(sha)
        self.filesystem.require_owned_layout()
        previous_sha = self.filesystem.read_current_sha()
        settings = settings_from_document(read_environment(self.paths.environment_file)[0])
        if previous_sha == revision:
            reconciled = DeploymentReconciler(
                self.filesystem,
                self.service,
                self.health,
            ).reconcile(self.paths.release(revision), settings)
            return UpdateResult(sha=revision, changed=reconciled.changed)
        if source is None:
            raise HostOperationError("The candidate source is required for an update.")

        # Include a successful pre-update reconciliation in the tmpfiles rollback.
        tmpfiles_snapshot = self.filesystem.snapshot(self.paths.tmpfiles_path)
        try:
            DeploymentReconciler(
                self.filesystem,
                self.service,
                self.health,
            ).reconcile(self.paths.release(previous_sha), settings)
            release = self.filesystem.prepare_release(source, revision, python)
            self.filesystem.validate_deployment_assets(release)
        except Exception as error:
            try:
                self.filesystem.restore_snapshot(self.paths.tmpfiles_path, tmpfiles_snapshot)
            except Exception:
                raise RollbackError("The previous runtime rule could not be restored.") from error
            raise
        previous_target = f"releases/{previous_sha}"
        unit_snapshot = self.filesystem.snapshot(self.paths.unit_path)
        try:
            self.filesystem.install_runtime_tmpfiles(release)
            self.filesystem.install_unit(release)
            # The unit may already match on disk after an interrupted update while
            # systemd still has its prior definition loaded.
            self.service.daemon_reload()
            self.filesystem.switch_current(revision)
            self.service.restart()
            verify_managed_health(
                self.service, self.health, settings.bind_host, settings.bind_port,
                settings.config_id, revision,
            )
            self.filesystem.install_stable_administration(release)
            return UpdateResult(sha=revision, changed=True)
        except Exception as error:
            rollback_errors: list[Exception] = [error] if isinstance(error, RollbackError) else []
            try:
                self.filesystem.restore_current(previous_target)
                self.filesystem.restore_snapshot(self.paths.unit_path, unit_snapshot)
                self.service.daemon_reload()
                self.service.restart()
                verify_managed_health(
                    self.service, self.health, settings.bind_host, settings.bind_port,
                    settings.config_id, previous_sha,
                )
            except Exception as rollback_error:
                rollback_errors.append(rollback_error)
            try:
                self.filesystem.restore_snapshot(self.paths.tmpfiles_path, tmpfiles_snapshot)
            except Exception as rollback_error:
                rollback_errors.append(rollback_error)
            if rollback_errors:
                raise RollbackError(
                    "Update failed and the previous manager release could not be verified healthy."
                ) from error
            raise TransactionFailedError(
                "Candidate update failed; the previous manager release was restored and is healthy."
            ) from error
