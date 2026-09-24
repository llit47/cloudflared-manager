"""Explicit installation of a separate local-ingress mutation sudo grant."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from cloudflared_manager.activation.barrier import ActivationRecoveryBarrier
from cloudflared_manager.deployment.bridge_install import BridgeInstaller, _production_boundary_check, _VISUDO
from cloudflared_manager.deployment.errors import HostOperationError, RollbackError
from cloudflared_manager.deployment.paths import DeploymentPaths
from cloudflared_manager.deployment.release import ReleaseFilesystem

_HELPER_ASSET = "deploy/privileged-mutation-helper.sh"
_SUDOERS_ASSET = "deploy/cloudflared-manager-mutation-bridge.sudoers"
_SUDOERS_POLICY = (
    b"Defaults:cloudflared-manager env_reset, !setenv\n"
    b'cloudflared-manager ALL=(root) NOPASSWD: /opt/cloudflared-manager/privileged-mutation-helper ""\n'
)


class MutationBridgeInstaller(BridgeInstaller):
    """Install no grant until root explicitly selects this separate command."""

    def __init__(self, paths: DeploymentPaths, filesystem: ReleaseFilesystem,
                 *, visudo: Path = _VISUDO,
                 boundary_check: Callable[[DeploymentPaths], None] | None = None,
                 barrier_check: Callable[[], None] | None = None) -> None:
        super().__init__(paths, filesystem, visudo=visudo,
                         boundary_check=boundary_check or _production_boundary_check)
        self.barrier_check = barrier_check or (lambda: ActivationRecoveryBarrier(paths).require_clean())

    def install(self, release: Path) -> bool:
        self.filesystem.validate_deployment_assets(release)
        self.barrier_check()
        self.boundary_check(self.paths)
        self._require_parent(self.paths.mutation_helper_path.parent)
        self._require_parent(self.paths.mutation_sudoers_path.parent)
        helper = self._source(release / _HELPER_ASSET)
        sudoers = self._source(release / _SUDOERS_ASSET)
        if sudoers != _SUDOERS_POLICY:
            raise HostOperationError("The active release has an unsafe mutation sudoers policy.")
        self.filesystem._validate_regular_asset(self.paths.mutation_helper_path, helper, _HELPER_ASSET)
        self.filesystem._validate_regular_asset(self.paths.mutation_sudoers_path, sudoers, _SUDOERS_ASSET)
        self._validate_sudoers(sudoers)
        helper_changed = not self.filesystem._regular_asset_matches(self.paths.mutation_helper_path, helper, 0o755)
        sudoers_changed = not self.filesystem._regular_asset_matches(self.paths.mutation_sudoers_path, sudoers, 0o440)
        tmpfiles_snapshot = self.filesystem.snapshot(self.paths.tmpfiles_path)
        helper_snapshot = self.filesystem.snapshot(self.paths.mutation_helper_path)
        sudoers_snapshot = self.filesystem.snapshot(self.paths.mutation_sudoers_path)
        attempted: list[tuple[Path, object]] = []
        try:
            attempted.append((self.paths.tmpfiles_path, tmpfiles_snapshot))
            runtime_changed = self.filesystem.install_runtime_tmpfiles(release)
            # The lock excludes manager updates; reject observable operator
            # release changes before either privilege-bearing asset is written.
            if self.filesystem.read_current_sha() != release.name:
                raise HostOperationError("The active manager release changed during bridge installation.")
            if helper_changed:
                attempted.append((self.paths.mutation_helper_path, helper_snapshot))
                self.filesystem.atomic_write(self.paths.mutation_helper_path, helper, 0o755)
            if not self.filesystem._regular_asset_matches(self.paths.mutation_helper_path, helper, 0o755):
                raise HostOperationError("The mutation helper could not be verified before granting sudo.")
            if self.filesystem.read_current_sha() != release.name:
                raise HostOperationError("The active manager release changed during bridge installation.")
            if sudoers_changed:
                attempted.append((self.paths.mutation_sudoers_path, sudoers_snapshot))
                self.filesystem.atomic_write(self.paths.mutation_sudoers_path, sudoers, 0o440)
            if not self.filesystem._regular_asset_matches(self.paths.mutation_sudoers_path, sudoers, 0o440):
                raise HostOperationError("The mutation sudoers policy could not be verified.")
            if self.filesystem.read_current_sha() != release.name:
                raise HostOperationError("The active manager release changed during bridge installation.")
        except Exception as error:
            rollback_failed = False
            for target, previous in reversed(attempted):
                try:
                    self.filesystem.restore_snapshot(target, previous)
                except Exception:
                    rollback_failed = True
            if rollback_failed:
                raise RollbackError("Mutation bridge installation rollback was incomplete.") from error
            raise
        return runtime_changed or helper_changed or sudoers_changed
