"""Disposable installer, migration, and privilege-boundary regressions."""

from pathlib import Path
import subprocess
import os

import pytest

from cloudflared_manager.deployment.errors import HostOperationError, RollbackError
from cloudflared_manager.deployment.mutation_bridge_install import MutationBridgeInstaller
from tests.deployment_support import make_paths
from tests.deployment_support import FakeService, fake_readiness, make_source
from tests.test_bridge_install import Filesystem
from tests.test_deployment_transactions import _installed
from cloudflared_manager.deployment.updater import Updater

ROOT = Path(__file__).parents[1]


class FakeFilesystem(Filesystem):
    def __init__(self, paths, release):
        super().__init__(paths)
        self.release = release

    def read_current_sha(self):
        return self.release.name


@pytest.fixture
def setup(tmp_path):
    paths = make_paths(tmp_path)
    paths.install_root.mkdir(parents=True)
    paths.sudoers_path.parent.mkdir(parents=True)
    paths.install_root.chmod(0o755)
    paths.sudoers_path.parent.chmod(0o755)
    release = tmp_path / ("a" * 40)
    (release / "deploy").mkdir(parents=True)
    for name in ("privileged-mutation-helper.sh", "cloudflared-manager-mutation-bridge.sudoers",
                 "cloudflared-manager.tmpfiles.conf"):
        (release / "deploy" / name).write_bytes((ROOT / "deploy" / name).read_bytes())
        (release / "deploy" / name).chmod(0o644)
    visudo = tmp_path / "visudo"
    visudo.write_text("#!/bin/sh\n[ \"$1\" = -cf ] && exit 0\nexit 1\n")
    visudo.chmod(0o755)
    return paths, release, visudo


def installer(setup, filesystem=None, **kwargs):
    paths, release, visudo = setup
    return MutationBridgeInstaller(paths, filesystem or FakeFilesystem(paths, release),
                                   visudo=visudo, boundary_check=lambda paths: None,
                                   barrier_check=lambda: None, **kwargs)


def test_explicit_install_is_idempotent_and_separate_from_recovery(setup):
    paths, release, _ = setup
    install = installer(setup)
    assert install.install(release)
    assert paths.mutation_helper_path.stat().st_mode & 0o777 == 0o755
    assert paths.mutation_sudoers_path.stat().st_mode & 0o777 == 0o440
    assert not paths.helper_path.exists()
    assert not paths.sudoers_path.exists()
    assert not install.install(release)


@pytest.mark.parametrize("failure", ["tmpfiles", "helper_before", "helper_after", "sudoers_before", "sudoers_after"])
def test_partial_install_rolls_back_in_reverse_order(setup, failure):
    paths, release, _ = setup
    class Failing(FakeFilesystem):
        def install_runtime_tmpfiles(self, release):
            if failure == "tmpfiles":
                raise HostOperationError("injected")
            return super().install_runtime_tmpfiles(release)
        def atomic_write(self, target, content, mode):
            if ((failure == "helper_before" and target == paths.mutation_helper_path)
                or (failure == "sudoers_before" and target == paths.mutation_sudoers_path)):
                raise HostOperationError("injected")
            super().atomic_write(target, content, mode)
            if ((failure == "helper_after" and target == paths.mutation_helper_path)
                or (failure == "sudoers_after" and target == paths.mutation_sudoers_path)):
                raise HostOperationError("injected")
    with pytest.raises(HostOperationError, match="injected"):
        installer(setup, Failing(paths, release)).install(release)
    assert not paths.mutation_helper_path.exists()
    assert not paths.mutation_sudoers_path.exists()
    assert not paths.tmpfiles_path.exists()


def test_incomplete_rollback_is_distinguished(setup):
    paths, release, _ = setup
    class Failing(FakeFilesystem):
        def atomic_write(self, target, content, mode):
            super().atomic_write(target, content, mode)
            if target == paths.mutation_sudoers_path:
                raise HostOperationError("injected")
        def restore_snapshot(self, target, previous):
            if target == paths.mutation_helper_path:
                raise HostOperationError("rollback failed")
            super().restore_snapshot(target, previous)
    with pytest.raises(RollbackError, match="incomplete"):
        installer(setup, Failing(paths, release)).install(release)
    assert not paths.mutation_sudoers_path.exists()


@pytest.mark.parametrize("target", ["helper", "sudoers"])
def test_unsafe_existing_target_is_rejected(setup, target):
    paths, release, _ = setup
    selected = paths.mutation_helper_path if target == "helper" else paths.mutation_sudoers_path
    selected.symlink_to(release / "deploy/privileged-mutation-helper.sh")
    with pytest.raises(HostOperationError):
        installer(setup).install(release)


def test_unsafe_asset_parent_or_sudoers_syntax_rejected(setup):
    paths, release, _ = setup
    paths.install_root.chmod(0o777)
    with pytest.raises(HostOperationError):
        installer(setup).install(release)
    paths.install_root.chmod(0o755)
    bad = release / "deploy/cloudflared-manager-mutation-bridge.sudoers"
    bad.write_text("invalid")
    bad_visudo = release / "visudo"
    bad_visudo.write_text("#!/bin/sh\nexit 1\n")
    bad_visudo.chmod(0o755)
    with pytest.raises(HostOperationError):
        MutationBridgeInstaller(paths, FakeFilesystem(paths, release), visudo=bad_visudo,
                                boundary_check=lambda paths: None,
                                barrier_check=lambda: None).install(release)
    assert not paths.mutation_sudoers_path.exists()


def test_unsafe_release_asset_and_sudoers_parent_rejected(setup):
    paths, release, _ = setup
    asset = release / "deploy/privileged-mutation-helper.sh"
    asset.chmod(0o666)
    with pytest.raises(HostOperationError):
        installer(setup).install(release)
    asset.chmod(0o644)
    paths.mutation_sudoers_path.parent.chmod(0o777)
    with pytest.raises(HostOperationError):
        installer(setup).install(release)
    assert not paths.mutation_helper_path.exists()


def test_launcher_is_fixed_root_only_and_web_unit_remains_read_only():
    helper = (ROOT / "deploy/privileged-mutation-helper.sh").read_text()
    unit = (ROOT / "deploy/cloudflared-manager.service").read_text()
    assert helper.startswith("#!/bin/bash -p\n")
    assert "[[ ${EUID} -ne 0 || $# -ne 0 ]]" in helper
    assert "/usr/bin/systemd-run --system --pipe --wait --quiet --collect" in helper
    assert "--property=ProtectSystem=strict" in helper
    assert "--property=RuntimeMaxSec=300s" in helper
    assert '"${manager_python}" -I -m cloudflared_manager.activation.mutation_helper' in helper
    assert "NoNewPrivileges=false" in unit
    assert "AmbientCapabilities=" in unit
    assert not any(line.startswith("ReadWritePaths=") for line in unit.splitlines())
    if os.geteuid() != 0:
        result = subprocess.run(["/bin/bash", "-p", str(ROOT / "deploy/privileged-mutation-helper.sh")],
                                capture_output=True, timeout=5, check=False, shell=False)
        assert result.returncode == 1


def test_current_release_change_before_grant_rolls_back(setup):
    paths, release, _ = setup
    class Stale(FakeFilesystem):
        def read_current_sha(self):
            return "b" * 40
    with pytest.raises(HostOperationError, match="release changed"):
        installer(setup, Stale(paths, release)).install(release)
    assert not paths.mutation_sudoers_path.exists()


def test_recovery_barrier_blocks_mutation_grant(setup):
    paths, release, visudo = setup
    def blocked():
        raise HostOperationError("recovery required")
    with pytest.raises(HostOperationError, match="recovery required"):
        MutationBridgeInstaller(paths, FakeFilesystem(paths, release), visudo=visudo,
                                boundary_check=lambda paths: None,
                                barrier_check=blocked).install(release)
    assert not paths.mutation_helper_path.exists()


def test_upgrade_and_reconciliation_have_no_implicit_mutation_grant():
    for module in ("installer.py", "updater.py", "reconciliation.py", "runtime_bootstrap.py"):
        source = (ROOT / "src/cloudflared_manager/deployment" / module).read_text()
        assert "MutationBridgeInstaller" not in source
        assert "mutation_sudoers_path" not in source
    old = (ROOT / "deploy/cloudflared-manager-bridge.sudoers").read_text()
    new = (ROOT / "deploy/cloudflared-manager-mutation-bridge.sudoers").read_text()
    assert "privileged-mutation-helper" not in old
    assert 'privileged-mutation-helper ""' in new
    assert "*" not in new


def test_upgrade_with_existing_recovery_bridge_does_not_install_mutation_grant(tmp_path):
    paths, filesystem = _installed(tmp_path)
    paths.helper_path.write_bytes((ROOT / "deploy/privileged-helper.sh").read_bytes())
    paths.sudoers_path.parent.mkdir(parents=True, exist_ok=True)
    paths.sudoers_path.write_bytes((ROOT / "deploy/cloudflared-manager-bridge.sudoers").read_bytes())
    source = make_source(tmp_path / "candidate")
    for name in ("privileged-mutation-helper.sh", "cloudflared-manager-mutation-bridge.sudoers"):
        (source / "deploy" / name).write_bytes((ROOT / "deploy" / name).read_bytes())
    service = FakeService()
    result = Updater(paths, filesystem, service,
                     lambda host, port: fake_readiness(host, port, release_id=filesystem.read_current_sha())).update(
                         source, "4" * 40, Path("/usr/bin/python3"))
    assert result.sha == "4" * 40
    assert paths.helper_path.exists() and paths.sudoers_path.exists()
    assert not paths.mutation_helper_path.exists()
    assert not paths.mutation_sudoers_path.exists()
