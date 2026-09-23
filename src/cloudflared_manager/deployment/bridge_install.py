"""Explicit root-administrator installation of the sole sudo bridge."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from cloudflared_manager.deployment.environment import read_environment, require_safe_environment
from cloudflared_manager.deployment.errors import HostOperationError
from cloudflared_manager.deployment.paths import DeploymentPaths
from cloudflared_manager.deployment.release import ReleaseFilesystem
from cloudflared_manager.deployment.settings import settings_from_document
from cloudflared_manager.deployment.write_boundary import require_write_boundary

_HELPER_ASSET = "deploy/privileged-helper.sh"
_SUDOERS_ASSET = "deploy/cloudflared-manager-bridge.sudoers"
_VISUDO = Path("/usr/sbin/visudo")


class BridgeInstaller:
    def __init__(self, paths: DeploymentPaths, filesystem: ReleaseFilesystem,
                 *, visudo: Path = _VISUDO,
                 boundary_check: Callable[[DeploymentPaths], None] | None = None) -> None:
        self.paths = paths
        self.filesystem = filesystem
        self.visudo = visudo
        self.boundary_check = boundary_check or _production_boundary_check

    def install(self, release: Path) -> bool:
        self.filesystem.validate_deployment_assets(release)
        runtime_changed = self.filesystem.install_runtime_tmpfiles(release)
        self.boundary_check(self.paths)
        self._require_parent(self.paths.helper_path.parent)
        self._require_parent(self.paths.sudoers_path.parent)
        helper = self._source(release / _HELPER_ASSET)
        sudoers = self._source(release / _SUDOERS_ASSET)
        self.filesystem._validate_regular_asset(self.paths.helper_path, helper, _HELPER_ASSET)
        self.filesystem._validate_regular_asset(self.paths.sudoers_path, sudoers, _SUDOERS_ASSET)
        self._validate_sudoers(sudoers)
        helper_changed = not self.filesystem._regular_asset_matches(self.paths.helper_path, helper, 0o755)
        sudoers_changed = not self.filesystem._regular_asset_matches(self.paths.sudoers_path, sudoers, 0o440)
        if helper_changed:
            self.filesystem.atomic_write(self.paths.helper_path, helper, 0o755)
        if sudoers_changed:
            self.filesystem.atomic_write(self.paths.sudoers_path, sudoers, 0o440)
        return runtime_changed or helper_changed or sudoers_changed

    def _source(self, path: Path) -> bytes:
        try:
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022
                or (self.filesystem.owner is not None
                    and (info.st_uid, info.st_gid) != self.filesystem.owner)):
                raise HostOperationError("The active release has an unsafe bridge asset.")
            value = path.read_bytes()
        except OSError as error:
            raise HostOperationError("The active release has an unavailable bridge asset.") from error
        if not value or len(value) > 16_384:
            raise HostOperationError("The active release has an unsafe bridge asset.")
        return value

    def _require_parent(self, path: Path) -> None:
        try:
            info = path.lstat()
        except OSError as error:
            raise HostOperationError("The bridge installation directory is unavailable.") from error
        if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022
            or (self.filesystem.owner is not None
                and (info.st_uid, info.st_gid) != self.filesystem.owner)):
            raise HostOperationError("The bridge installation directory is unsafe.")

    def _validate_sudoers(self, content: bytes) -> None:
        name: str | None = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=".cfm-bridge.", dir=self.paths.sudoers_path.parent)
            os.fchmod(descriptor, 0o440)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            result = subprocess.run([str(self.visudo), "-cf", name],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=10, check=False, shell=False)
            if result.returncode != 0:
                raise HostOperationError("The bridge sudoers policy failed syntax validation.")
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HostOperationError("The bridge sudoers policy could not be validated.") from error
        finally:
            if name is not None:
                os.unlink(name)


def _production_boundary_check(paths: DeploymentPaths) -> None:
    require_safe_environment(paths.environment_file, owner=(0, 0))
    settings = settings_from_document(read_environment(paths.environment_file)[0])
    require_write_boundary(paths, adopted=settings.cloudflared_config_path)
