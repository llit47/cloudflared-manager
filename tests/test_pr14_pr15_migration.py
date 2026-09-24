"""First PR15 upgrade driven by the updater in PR14 commit 49f8c41a."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from cloudflared_manager.deployment.environment import atomic_write_environment, initial_environment
from cloudflared_manager.deployment.errors import HostOperationError, TransactionFailedError
from cloudflared_manager.deployment.release import ReleaseFilesystem
from cloudflared_manager.deployment import runtime_bootstrap
from cloudflared_manager.deployment.runtime_bootstrap import install_current_runtime_rule
from cloudflared_manager.deployment.updater import Updater
from cloudflared_manager.deployment.write_boundary import require_write_boundary
from tests.deployment_support import (
    FakePreparationRunner, FakeService, fake_readiness, make_paths, make_source,
)
from tests.fixtures.pr14_updater import Updater as PR14Updater
from tests.fixtures.pr14_release import PR14ReleaseFilesystem

ROOT = Path(__file__).parents[1]
OLD = "3" * 40
NEW = "4" * 40
RULE = b"d /run/cloudflared-manager 0700 root root -\n"


class LegacyService(FakeService):
    def __init__(self, paths, candidate_filesystem):
        super().__init__()
        self.paths = paths
        self.candidate_filesystem = candidate_filesystem
        self.bootstrap_calls = 0

    def restart(self):
        self.calls.append("restart")
        if b"cloudflared_manager.deployment.runtime_bootstrap" in self.paths.unit_path.read_bytes():
            # Only after PR14 switches current and restarts does systemd run
            # the PR15 candidate unit's fixed pre-start transient service.
            assert self.candidate_filesystem.read_current_sha() == NEW
            if self.bootstrap_calls == 0:
                assert not self.paths.tmpfiles_path.exists()
            self.bootstrap_calls += 1
            install_current_runtime_rule(self.paths, self.candidate_filesystem)
        self.active = True
        self.main_pid = 1234


def _setup(tmp_path):
    paths = make_paths(tmp_path)
    filesystem = PR14ReleaseFilesystem(paths, owner=None, process_runner=FakePreparationRunner())
    candidate_filesystem = ReleaseFilesystem(paths, owner=None,
                                             process_runner=FakePreparationRunner())
    filesystem.ensure_layout()
    paths.config_root.parent.chmod(0o755)
    old_source = make_source(tmp_path / "old", unit=b"[Service]\nType=simple\n# PR14\n",
                             administration_version="pr14")
    (old_source / "deploy/cloudflared-manager.tmpfiles.conf").unlink()
    old_release = filesystem.prepare_release(old_source, OLD, Path("/usr/bin/python3"))
    filesystem.switch_current(OLD)
    filesystem.install_unit(old_release)
    filesystem.install_stable_administration(old_release)
    atomic_write_environment(paths.environment_file,
                             initial_environment("192.168.1.20", 8000), owner=None)
    assert not paths.tmpfiles_path.exists()
    candidate_source = make_source(
        tmp_path / "candidate",
        unit=(ROOT / "deploy/cloudflared-manager.service").read_bytes(),
        administration_version="pr15",
    )
    return paths, filesystem, candidate_filesystem, candidate_source


def _health(filesystem):
    return lambda host, port: fake_readiness(
        host, port, release_id=filesystem.read_current_sha()
    )


def _boot_and_check(paths, tmp_path):
    assert paths.tmpfiles_path.read_bytes() == RULE
    result = subprocess.run([str(paths.tmpfiles_executable), "--create",
                             str(paths.tmpfiles_path)], check=False, timeout=10)
    assert result.returncode == 0
    paths.runtime_root.parent.chmod(0o755)
    assert paths.runtime_root.stat().st_mode & 0o777 == 0o700
    require_write_boundary(paths, adopted=None,
                           cloudflared_root=tmp_path / "etc/cloudflared",
                           trusted_uid=os.getuid(), service_uid=os.getuid() + 1,
                           anchor=tmp_path)


def test_pr14_updater_first_upgrade_installs_boot_rule_before_restart(tmp_path):
    paths, filesystem, candidate_filesystem, source = _setup(tmp_path)
    service = LegacyService(paths, candidate_filesystem)
    PR14Updater(paths, filesystem, service, _health(filesystem)).update(
        source, NEW, Path("/usr/bin/python3")
    )
    assert filesystem.read_current_sha() == NEW
    assert service.bootstrap_calls == 1
    assert paths.tmpfiles_path.read_bytes() == RULE
    assert paths.tmpfiles_path.stat().st_mode & 0o777 == 0o644
    assert paths.runtime_root.stat().st_mode & 0o777 == 0o700

    shutil.rmtree(paths.runtime_root.parent)
    _boot_and_check(paths, tmp_path)
    shutil.rmtree(paths.runtime_root.parent)
    _boot_and_check(paths, tmp_path)
    service.restart()
    assert service.bootstrap_calls == 2
    assert paths.tmpfiles_path.read_bytes() == RULE
    assert not Updater(paths, candidate_filesystem, service, _health(filesystem)).update(
        None, NEW, Path("/usr/bin/python3")
    ).changed


def test_pr14_updater_rolls_back_when_bootstrap_fails(tmp_path):
    paths, filesystem, candidate_filesystem, source = _setup(tmp_path)
    paths.tmpfiles_executable.write_text("#!/bin/sh\nexit 1\n")
    service = LegacyService(paths, candidate_filesystem)
    with pytest.raises(TransactionFailedError, match="restored"):
        PR14Updater(paths, filesystem, service, _health(filesystem)).update(
            source, NEW, Path("/usr/bin/python3")
        )
    assert filesystem.read_current_sha() == OLD
    assert b"# PR14" in paths.unit_path.read_bytes()
    assert not paths.tmpfiles_path.exists()


def test_pr14_health_failure_restores_old_release_with_safe_boot_rule(tmp_path):
    paths, filesystem, candidate_filesystem, source = _setup(tmp_path)
    service = LegacyService(paths, candidate_filesystem)
    def fail_new(host, port):
        if filesystem.read_current_sha() == NEW:
            raise HostOperationError("candidate health failed")
        return _health(filesystem)(host, port)

    with pytest.raises(TransactionFailedError, match="restored"):
        PR14Updater(paths, filesystem, service, fail_new).update(
            source, NEW, Path("/usr/bin/python3")
        )
    assert filesystem.read_current_sha() == OLD
    assert b"# PR14" in paths.unit_path.read_bytes()
    # PR14 has no cleanup hook for the additive root-owned rule; it remains
    # compatible with PR14 and the directory is still safe after reboot.
    shutil.rmtree(paths.runtime_root.parent)
    _boot_and_check(paths, tmp_path)


def test_pr14_upgrade_rejects_unsafe_existing_runtime_directory(tmp_path):
    paths, filesystem, candidate_filesystem, source = _setup(tmp_path)
    paths.runtime_root.mkdir(parents=True)
    paths.runtime_root.chmod(0o777)
    service = LegacyService(paths, candidate_filesystem)
    with pytest.raises(TransactionFailedError, match="restored"):
        PR14Updater(paths, filesystem, service, _health(filesystem)).update(
            source, NEW, Path("/usr/bin/python3")
        )
    assert filesystem.read_current_sha() == OLD
    assert paths.runtime_root.stat().st_mode & 0o777 == 0o777
    assert not paths.tmpfiles_path.exists()


def test_candidate_unit_bootstrap_is_fixed_and_web_mount_stays_read_only():
    unit = (ROOT / "deploy/cloudflared-manager.service").read_text()
    prestart = next(line for line in unit.splitlines() if line.startswith("ExecStartPre="))
    assert prestart.startswith("ExecStartPre=!/usr/bin/env -i ")
    assert "/usr/bin/systemd-run --system --pipe --wait --quiet --collect" in prestart
    assert "--property=ProtectSystem=strict" in prestart
    assert "--property=ReadWritePaths=/etc/tmpfiles.d /run" in prestart
    assert "--property=RuntimeMaxSec=30s" in prestart
    assert "--property=CapabilityBoundingSet=CAP_CHOWN" in prestart
    assert prestart.endswith(
        "/opt/cloudflared-manager/current/.venv/bin/python -I -m "
        "cloudflared_manager.deployment.runtime_bootstrap"
    )
    assert "ExecStartPre=+" not in unit
    assert "$" not in prestart and "/bin/sh" not in prestart
    assert "ProtectSystem=strict" in unit
    assert not any(line.startswith("ReadWritePaths=") for line in unit.splitlines())


def test_runtime_bootstrap_rejects_nonroot_and_arguments(monkeypatch):
    monkeypatch.setattr(runtime_bootstrap.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(runtime_bootstrap.sys, "argv", ["runtime_bootstrap"])
    assert runtime_bootstrap.main() == 1
    monkeypatch.setattr(runtime_bootstrap.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runtime_bootstrap.sys, "argv", ["runtime_bootstrap", "/etc/passwd"])
    assert runtime_bootstrap.main() == 1
