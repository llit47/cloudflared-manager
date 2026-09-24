"""Crash recovery for one recognized active manager deployment."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cloudflared_manager.deployment.errors import RollbackError, TransactionFailedError
from cloudflared_manager.deployment.health import verify_managed_health
from cloudflared_manager.deployment.protocols import HealthVerifier, ManagerService
from cloudflared_manager.deployment.release import ReleaseFilesystem
from cloudflared_manager.deployment.settings import ManagerSettings


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    changed: bool


class DeploymentReconciler:
    """Repair only assets proven to belong to the active manager release."""

    def __init__(
        self,
        filesystem: ReleaseFilesystem,
        service: ManagerService,
        health: HealthVerifier,
    ) -> None:
        self.filesystem = filesystem
        self.service = service
        self.health = health

    def reconcile(self, release: Path, settings: ManagerSettings) -> ReconcileResult:
        self.filesystem.validate_deployment_assets(release)
        tmpfiles_snapshot = self.filesystem.snapshot(self.filesystem.paths.tmpfiles_path)
        unit_snapshot = self.filesystem.snapshot(self.filesystem.paths.unit_path)
        initial_runtime = self.service.runtime_state()
        was_active = initial_runtime.active
        was_enabled = self.service.is_enabled()
        unit_install_attempted = False
        unit_install_completed = False
        unit_changed = False
        unit_reload_attempted = False
        unit_repair_verified = False
        service_changed = False
        service_operation_attempted = False
        enable_attempted = False
        runtime_changed = False
        try:
            runtime_changed = self.filesystem.install_runtime_tmpfiles(release)
            unit_install_attempted = True
            unit_changed = self.filesystem.install_unit(release)
            unit_install_completed = True
            unit_sync_required = unit_changed or initial_runtime.needs_daemon_reload
            if unit_sync_required:
                unit_reload_attempted = True
                self.service.daemon_reload()
                service_operation_attempted = True
                if was_active:
                    self.service.restart()
                else:
                    self.service.start()
                service_changed = True
                self._verify_health(settings, release.name)
                unit_repair_verified = True
            elif not was_active:
                service_operation_attempted = True
                self.service.start()
                service_changed = True
                self._verify_health(settings, release.name)
            else:
                try:
                    self._verify_health(settings, release.name)
                except Exception:
                    service_operation_attempted = True
                    self.service.restart()
                    service_changed = True
                    self._verify_health(settings, release.name)

            if not was_enabled:
                enable_attempted = True
                self.service.enable()
                service_changed = True
            administration_changed = self.filesystem.install_stable_administration(release)
            return ReconcileResult(
                changed=runtime_changed or unit_changed or service_changed or administration_changed
            )
        except Exception as error:
            rollback_errors: list[Exception] = [error] if isinstance(error, RollbackError) else []
            if enable_attempted:
                try:
                    self.service.disable()
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
            if (
                not unit_repair_verified
                and (unit_changed or (unit_install_attempted and not unit_install_completed))
            ):
                try:
                    self.filesystem.restore_snapshot(
                        self.filesystem.paths.unit_path,
                        unit_snapshot,
                    )
                    if unit_reload_attempted:
                        self.service.daemon_reload()
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
            if service_operation_attempted:
                try:
                    if was_active:
                        self.service.restart()
                        self._verify_health(settings, release.name)
                    else:
                        self.service.stop()
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
            if runtime_changed:
                try:
                    self.filesystem.restore_snapshot(
                        self.filesystem.paths.tmpfiles_path, tmpfiles_snapshot
                    )
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
            if rollback_errors:
                raise RollbackError(
                    "Manager deployment reconciliation failed and rollback was incomplete."
                ) from error
            raise TransactionFailedError(
                "Manager deployment reconciliation failed after safe manager recovery."
            ) from error

    def _verify_health(self, settings: ManagerSettings, release_id: str) -> None:
        verify_managed_health(
            self.service,
            self.health,
            settings.bind_host,
            settings.bind_port,
            settings.config_id,
            release_id,
        )
