"""PR14 release behavior changed by PR15, frozen for migration tests.

The remaining ReleaseFilesystem methods were unchanged between PR14 commit
49f8c41a and PR15. Keep these overrides aligned with that commit so the old
updater cannot accidentally use PR15 tmpfiles validation or installation.
"""

from __future__ import annotations

from pathlib import Path

from cloudflared_manager.deployment.errors import HostOperationError
from cloudflared_manager.deployment.release import ReleaseFilesystem


class PR14ReleaseFilesystem(ReleaseFilesystem):
    def _validate_source(self, source: Path) -> None:
        if source.is_symlink() or not source.is_dir():
            raise HostOperationError("The candidate source directory is unsafe.")
        if any(path.is_symlink() for path in source.rglob("*")):
            raise HostOperationError("The candidate source contains an unsupported symlink.")
        for relative in (
            "pyproject.toml",
            "deploy/cloudflared-manager.service",
            "deploy/update.sh",
            "deploy/config.sh",
        ):
            path = source / relative
            if path.is_symlink() or not path.is_file():
                raise HostOperationError("The candidate source layout is incomplete.")

    def validate_deployment_assets(self, release: Path) -> None:
        self.require_owned_layout()
        self._require_ready_release(release)
        candidates = (
            (
                self.paths.unit_path,
                release / "deploy" / "cloudflared-manager.service",
                "deploy/cloudflared-manager.service",
            ),
            (self.paths.stable_update, release / "deploy" / "update.sh", "deploy/update.sh"),
            (self.paths.stable_config, release / "deploy" / "config.sh", "deploy/config.sh"),
        )
        for target, candidate, relative in candidates:
            self._validate_regular_asset(target, candidate.read_bytes(), relative)
        self._validate_command_link(self.paths.update_link, self.paths.stable_update)
        self._validate_command_link(self.paths.config_link, self.paths.stable_config)

    def install_runtime_tmpfiles(self, release: Path) -> bool:
        raise AssertionError("PR14 cannot install the PR15 runtime rule")
