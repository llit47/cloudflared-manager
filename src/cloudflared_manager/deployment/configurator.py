"""Safe manager EnvironmentFile configuration transaction."""

from __future__ import annotations

from dataclasses import dataclass

from cloudflared_manager.deployment.environment import atomic_write_environment, read_environment
from cloudflared_manager.deployment.errors import RollbackError, TransactionFailedError
from cloudflared_manager.deployment.health import verify_managed_health
from cloudflared_manager.deployment.paths import DeploymentPaths
from cloudflared_manager.deployment.protocols import HealthVerifier, ManagerService
from cloudflared_manager.deployment.settings import ManagerSettings, settings_from_document


@dataclass(frozen=True, slots=True)
class ConfigResult:
    changed: bool
    settings: ManagerSettings


class Configurator:
    """Read and transactionally update supported manager configuration."""

    def __init__(
        self,
        paths: DeploymentPaths,
        service: ManagerService,
        health: HealthVerifier,
        *,
        environment_owner: tuple[int, int] | None = (0, 0),
    ) -> None:
        self.paths = paths
        self.service = service
        self.health = health
        self.environment_owner = environment_owner

    def status(self) -> tuple[ManagerSettings, tuple[str | None, str | None, str | None]]:
        document, _ = read_environment(self.paths.environment_file)
        return settings_from_document(document), self.service.sanitized_status()

    def apply(self, updates: dict[str, str]) -> ConfigResult:
        document, previous = read_environment(self.paths.environment_file)
        previous_settings = settings_from_document(document)
        candidate = document.updated(updates)
        candidate_settings = settings_from_document(candidate)
        if candidate.render() == document.render():
            try:
                verify_managed_health(
                    self.service, self.health, previous_settings.bind_host,
                    previous_settings.bind_port, previous_settings.config_id,
                )
                return ConfigResult(changed=False, settings=previous_settings)
            except Exception:
                self.service.restart()
                verify_managed_health(
                    self.service, self.health, previous_settings.bind_host,
                    previous_settings.bind_port, previous_settings.config_id,
                )
                return ConfigResult(changed=True, settings=previous_settings)

        try:
            atomic_write_environment(
                self.paths.environment_file,
                candidate,
                owner=self.environment_owner,
            )
            self.service.restart()
            verify_managed_health(
                self.service,
                self.health,
                candidate_settings.bind_host,
                candidate_settings.bind_port,
                candidate_settings.config_id,
            )
            return ConfigResult(changed=True, settings=candidate_settings)
        except Exception as error:
            try:
                atomic_write_environment(
                    self.paths.environment_file,
                    previous,
                    owner=self.environment_owner,
                )
                self.service.restart()
                verify_managed_health(
                    self.service,
                    self.health,
                    previous_settings.bind_host,
                    previous_settings.bind_port,
                    previous_settings.config_id,
                )
            except Exception as rollback_error:
                raise RollbackError(
                    "Configuration failed and the previous manager configuration is not healthy."
                ) from rollback_error
            raise TransactionFailedError(
                "Configuration failed; the previous configuration was restored and is healthy."
            ) from error
