"""Fixed root bootstrap consumed by the PR14 updater via the candidate unit."""

from __future__ import annotations

import os
import sys

from cloudflared_manager.deployment.paths import DeploymentPaths
from cloudflared_manager.deployment.release import ReleaseFilesystem


def install_current_runtime_rule(paths: DeploymentPaths, filesystem: ReleaseFilesystem) -> None:
    """Use the current root-owned release and existing validated tmpfiles installer."""

    revision = filesystem.read_current_sha()
    filesystem.install_runtime_tmpfiles(paths.release(revision))


def main() -> int:
    if os.geteuid() != 0 or len(sys.argv) != 1:
        return 1
    os.umask(0o077)
    paths = DeploymentPaths()
    try:
        install_current_runtime_rule(paths, ReleaseFilesystem(paths))
    except Exception:
        # No host path, release identity, rule content, or subprocess output leaks.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
