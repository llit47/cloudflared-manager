"""Explicit root installation contract with disposable paths and fake visudo."""

from pathlib import Path

import pytest

from cloudflared_manager.deployment.bridge_install import BridgeInstaller
from cloudflared_manager.deployment.errors import HostOperationError
from tests.deployment_support import make_paths


class Filesystem:
    owner = None
    def validate_deployment_assets(self, release):
        pass
    def _validate_regular_asset(self, target, candidate, relative):
        if target.is_symlink():
            raise HostOperationError("unsafe")
    def _regular_asset_matches(self, target, content, mode):
        return target.exists() and target.read_bytes() == content and target.stat().st_mode & 0o777 == mode
    def atomic_write(self, target, content, mode):
        target.write_bytes(content)
        target.chmod(mode)


@pytest.fixture
def setup(tmp_path):
    paths = make_paths(tmp_path)
    paths.install_root.mkdir(parents=True)
    paths.sudoers_path.parent.mkdir(parents=True)
    paths.install_root.chmod(0o755)
    paths.sudoers_path.parent.chmod(0o755)
    release = tmp_path / "release"
    (release / "deploy").mkdir(parents=True)
    (release / "deploy/privileged-helper.sh").write_text("#!/bin/bash\nexit 0\n")
    (release / "deploy/cloudflared-manager-bridge.sudoers").write_text(
        'cloudflared-manager ALL=(root) NOPASSWD: /opt/cloudflared-manager/privileged-helper ""\n')
    (release / "deploy/privileged-helper.sh").chmod(0o644)
    (release / "deploy/cloudflared-manager-bridge.sudoers").chmod(0o644)
    visudo = tmp_path / "visudo"
    visudo.write_text("#!/bin/sh\n[ \"$1\" = -cf ] && exit 0\nexit 1\n")
    visudo.chmod(0o755)
    return paths, release, visudo


def test_explicit_bridge_install_is_idempotent_and_restrictive(setup):
    paths, release, visudo = setup
    installer = BridgeInstaller(paths, Filesystem(), visudo=visudo)
    assert installer.install(release)
    assert paths.helper_path.stat().st_mode & 0o777 == 0o755
    assert paths.sudoers_path.stat().st_mode & 0o777 == 0o440
    assert not installer.install(release)


def test_bridge_install_rejects_symlink_target(setup):
    paths, release, visudo = setup
    paths.helper_path.symlink_to(release / "deploy/privileged-helper.sh")
    with pytest.raises(HostOperationError):
        BridgeInstaller(paths, Filesystem(), visudo=visudo).install(release)


def test_bridge_install_fails_before_install_when_visudo_unavailable(setup):
    paths, release, _ = setup
    with pytest.raises(HostOperationError):
        BridgeInstaller(paths, Filesystem(), visudo=Path("/missing/visudo")).install(release)
    assert not paths.helper_path.exists()
    assert not paths.sudoers_path.exists()


def test_sudoers_grants_exact_helper_with_no_arguments():
    policy = (Path(__file__).parents[1] / "deploy/cloudflared-manager-bridge.sudoers").read_text()
    assert 'NOPASSWD: /opt/cloudflared-manager/privileged-helper ""' in policy
    assert "ALL=(ALL)" not in policy
    assert "*" not in policy
