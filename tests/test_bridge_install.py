"""Explicit root installation contract with disposable paths and fake visudo."""

import shlex
import subprocess
from pathlib import Path

import pytest

from cloudflared_manager.deployment.bridge_install import BridgeInstaller
from cloudflared_manager.deployment.errors import HostOperationError
from cloudflared_manager.deployment.release import PathSnapshot
from tests.deployment_support import make_paths


class Filesystem:
    owner = None
    def __init__(self, paths):
        self.paths = paths
    def validate_deployment_assets(self, release):
        pass
    def snapshot(self, target):
        return (PathSnapshot(kind="file", content=target.read_bytes(),
                             mode=target.stat().st_mode & 0o777)
                if target.exists() else PathSnapshot(kind="missing"))
    def restore_snapshot(self, target, previous):
        if previous.kind == "missing":
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(previous.content)
            target.chmod(previous.mode)
    def install_runtime_tmpfiles(self, release):
        target = self.paths.tmpfiles_path
        target.parent.mkdir(parents=True, exist_ok=True)
        content = (release / "deploy/cloudflared-manager.tmpfiles.conf").read_bytes()
        changed = not target.exists() or target.read_bytes() != content or not self.paths.runtime_root.exists()
        target.write_bytes(content)
        target.chmod(0o644)
        self.paths.runtime_root.mkdir(parents=True, exist_ok=True)
        self.paths.runtime_root.chmod(0o700)
        return changed
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
    (release / "deploy/cloudflared-manager.tmpfiles.conf").write_text(
        "d /run/cloudflared-manager 0700 root root -\n")
    (release / "deploy/privileged-helper.sh").chmod(0o644)
    (release / "deploy/cloudflared-manager-bridge.sudoers").chmod(0o644)
    visudo = tmp_path / "visudo"
    visudo.write_text("#!/bin/sh\n[ \"$1\" = -cf ] && exit 0\nexit 1\n")
    visudo.chmod(0o755)
    return paths, release, visudo


def test_explicit_bridge_install_is_idempotent_and_restrictive(setup):
    paths, release, visudo = setup
    installer = BridgeInstaller(paths, Filesystem(paths), visudo=visudo,
                                boundary_check=lambda paths: None)
    assert installer.install(release)
    assert paths.helper_path.stat().st_mode & 0o777 == 0o755
    assert paths.sudoers_path.stat().st_mode & 0o777 == 0o440
    assert paths.tmpfiles_path.read_text() == "d /run/cloudflared-manager 0700 root root -\n"
    assert paths.runtime_root.stat().st_mode & 0o777 == 0o700
    assert not installer.install(release)


def test_bridge_install_rejects_symlink_target(setup):
    paths, release, visudo = setup
    paths.helper_path.symlink_to(release / "deploy/privileged-helper.sh")
    with pytest.raises(HostOperationError):
        BridgeInstaller(paths, Filesystem(paths), visudo=visudo,
                        boundary_check=lambda paths: None).install(release)


def test_bridge_install_fails_before_install_when_visudo_unavailable(setup):
    paths, release, _ = setup
    with pytest.raises(HostOperationError):
        BridgeInstaller(paths, Filesystem(paths), visudo=Path("/missing/visudo"),
                        boundary_check=lambda paths: None).install(release)
    assert not paths.helper_path.exists()
    assert not paths.sudoers_path.exists()


def test_bridge_install_rejects_unsafe_write_boundary_before_grant(setup):
    paths, release, visudo = setup
    def unsafe(_):
        raise HostOperationError("unsafe write boundary")
    with pytest.raises(HostOperationError, match="unsafe write boundary"):
        BridgeInstaller(paths, Filesystem(paths), visudo=visudo,
                        boundary_check=unsafe).install(release)
    assert not paths.helper_path.exists()
    assert not paths.sudoers_path.exists()


def test_failed_bridge_install_restores_prior_tmpfiles_rule(setup):
    paths, release, visudo = setup
    paths.tmpfiles_path.parent.mkdir(parents=True)
    prior = (release / "deploy/cloudflared-manager.tmpfiles.conf").read_bytes()
    paths.tmpfiles_path.write_bytes(prior)
    paths.tmpfiles_path.chmod(0o600)

    class FailedHelperWrite(Filesystem):
        def atomic_write(self, target, content, mode):
            if target == paths.helper_path:
                raise HostOperationError("helper installation failed")
            super().atomic_write(target, content, mode)

    with pytest.raises(HostOperationError, match="helper installation failed"):
        BridgeInstaller(paths, FailedHelperWrite(paths), visudo=visudo,
                        boundary_check=lambda paths: None).install(release)
    assert paths.tmpfiles_path.read_bytes() == prior
    assert paths.tmpfiles_path.stat().st_mode & 0o777 == 0o600


def test_sudoers_grants_exact_helper_with_no_arguments():
    policy = (Path(__file__).parents[1] / "deploy/cloudflared-manager-bridge.sudoers").read_text()
    assert 'NOPASSWD: /opt/cloudflared-manager/privileged-helper ""' in policy
    assert "ALL=(ALL)" not in policy
    assert "*" not in policy


def test_privileged_launcher_ignores_hostile_path_and_bash_startup(tmp_path):
    """Exercise the real launcher below the root gate against pre-env injection."""
    source = (Path(__file__).parents[1] / "deploy/privileged-helper.sh").read_text()
    assert source.startswith("#!/bin/bash -p\n")
    assert source.count("/usr/bin/readlink --no-newline") == 1
    assert source.count("[[ ${EUID} -ne 0 || $# -ne 0 ]]") == 1
    assert source.count("readonly install_root='/opt/cloudflared-manager'") == 1

    root = tmp_path / "install"
    release = root / "releases" / ("a" * 40)
    python = release / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nprintf 'SAFE\\n'\n")
    python.chmod(0o755)
    (root / "current").symlink_to(f"releases/{release.name}")

    # A disposable copy changes only the root guard and fixed install root, so
    # CI can execute the privileged path without root or production paths.
    test_source = source.replace("[[ ${EUID} -ne 0 || $# -ne 0 ]]", "[[ $# -ne 0 ]]", 1)
    test_source = test_source.replace(
        "readonly install_root='/opt/cloudflared-manager'",
        f"readonly install_root={shlex.quote(str(root))}", 1,
    )
    runner = tmp_path / "systemd-run"
    runner.write_text(
        "#!/bin/bash\n"
        "[[ \" $* \" == *' --system --pipe --wait --quiet --collect '* ]] || exit 91\n"
        "[[ \" $* \" == *' --property=ProtectSystem=strict '* ]] || exit 92\n"
        "[[ \" $* \" == *' --property=ReadWritePaths=/etc/cloudflared /etc/cloudflared-manager /run/cloudflared-manager '* ]] || exit 93\n"
        "exec \"${@: -4}\"\n"
    )
    runner.chmod(0o755)
    test_source = test_source.replace("/usr/bin/systemd-run", str(runner), 1)
    assert test_source != source and "${EUID}" not in test_source
    launcher = tmp_path / "privileged-helper"
    launcher.write_text(test_source)
    launcher.chmod(0o755)

    marker = tmp_path / "injected"
    startup = tmp_path / "startup.sh"
    startup.write_text('printf "startup" > "$ATTACK_MARKER"\n')
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_readlink = fake_bin / "readlink"
    fake_readlink.write_text('#!/bin/sh\nprintf "path" > "$ATTACK_MARKER"\nexit 98\n')
    fake_readlink.chmod(0o755)
    environment = {
        "PATH": str(fake_bin), "BASH_ENV": str(startup), "ATTACK_MARKER": str(marker),
    }
    completed = subprocess.run([str(launcher)], env=environment,
                               capture_output=True, timeout=5, check=False)
    assert completed.returncode == 0
    assert completed.stdout == b"SAFE\n"
    assert not marker.exists()


def test_recovery_uses_isolated_system_manager_mount_with_fixed_authority():
    root = Path(__file__).parents[1]
    web = (root / "deploy/cloudflared-manager.service").read_text()
    helper = (root / "deploy/privileged-helper.sh").read_text()
    assert "ProtectSystem=strict" in web
    assert "ReadWritePaths=/etc/cloudflared-manager /run/cloudflared-manager" in web
    assert not any("/etc/cloudflared" in line.split("=", 1)[1].split()
                   for line in web.splitlines() if line.startswith("ReadWritePaths="))
    assert "/usr/bin/systemd-run --system --pipe --wait --quiet --collect" in helper
    assert "--property=ProtectSystem=strict" in helper
    assert "--property=ReadWritePaths=/etc/cloudflared /etc/cloudflared-manager /run/cloudflared-manager" in helper
    assert '"${manager_python}" -I -m cloudflared_manager.activation.bridge_helper' in helper
    assert "CAP_SYS_ADMIN" not in web + helper
