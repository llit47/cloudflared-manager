"""Boot and immediate runtime-directory installation on disposable paths."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloudflared_manager.deployment.errors import HostOperationError
from cloudflared_manager.deployment.paths import DeploymentPaths
from cloudflared_manager.deployment.release import ReleaseFilesystem
from tests.deployment_support import FakePreparationRunner, make_paths, make_source

SHA = "e" * 40
RULE = b"d /run/cloudflared-manager 0700 root root -\n"


@pytest.fixture
def prepared(tmp_path):
    paths = make_paths(tmp_path)
    filesystem = ReleaseFilesystem(paths, owner=None, process_runner=FakePreparationRunner())
    filesystem.ensure_layout()
    release = filesystem.prepare_release(make_source(tmp_path / "source"), SHA,
                                         Path("/usr/bin/python3"))
    return paths, filesystem, release


def test_boot_rule_has_exact_root_owned_private_directory_contract():
    assert DeploymentPaths().tmpfiles_path == Path("/etc/tmpfiles.d/cloudflared-manager.conf")
    assert (Path(__file__).parents[1] / "deploy/cloudflared-manager.tmpfiles.conf").read_bytes() == RULE


def test_install_applies_rule_immediately_and_recreates_cleared_run(prepared):
    paths, filesystem, release = prepared
    assert filesystem.install_runtime_tmpfiles(release)
    assert paths.tmpfiles_path.read_bytes() == RULE
    assert paths.tmpfiles_path.stat().st_mode & 0o777 == 0o644
    assert paths.runtime_root.is_dir()
    assert paths.runtime_root.stat().st_mode & 0o777 == 0o700
    assert not filesystem.install_runtime_tmpfiles(release)

    # A reboot clears /run; the installed tmpfiles rule is durable and can
    # recreate the directory before the manager unit starts.
    shutil.rmtree(paths.runtime_root.parent)
    assert filesystem.install_runtime_tmpfiles(release)
    assert paths.runtime_root.is_dir()
    assert paths.runtime_root.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("unsafe", ["mode", "symlink"])
def test_unsafe_preexisting_runtime_path_is_not_repaired(prepared, unsafe):
    paths, filesystem, release = prepared
    paths.runtime_root.parent.mkdir(parents=True)
    paths.runtime_root.parent.chmod(0o755)
    if unsafe == "mode":
        paths.runtime_root.mkdir(mode=0o777)
        paths.runtime_root.chmod(0o777)
    else:
        paths.runtime_root.symlink_to(paths.config_root, target_is_directory=True)
    with pytest.raises(HostOperationError, match="runtime directory is unsafe"):
        filesystem.install_runtime_tmpfiles(release)
    assert not paths.tmpfiles_path.exists()


def test_tmpfiles_application_uses_fixed_argv_and_rechecks_metadata(prepared, monkeypatch):
    from cloudflared_manager.deployment import release as release_module

    paths, filesystem, candidate = prepared
    calls = []
    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        paths.runtime_root.mkdir(parents=True)
        paths.runtime_root.chmod(0o700)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(release_module.subprocess, "run", runner)
    assert filesystem.install_runtime_tmpfiles(candidate)
    argv, kwargs = calls[0]
    assert argv == [str(paths.tmpfiles_executable), "--create", str(paths.tmpfiles_path)]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 10
    assert kwargs["stdin"] == release_module.subprocess.DEVNULL


def test_tmpfiles_application_failure_is_reported(prepared, monkeypatch):
    from cloudflared_manager.deployment import release as release_module

    paths, filesystem, candidate = prepared
    monkeypatch.setattr(release_module.subprocess, "run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(HostOperationError, match="could not be created"):
        filesystem.install_runtime_tmpfiles(candidate)
    assert not paths.runtime_root.exists()


def test_release_rejects_altered_tmpfiles_rule(prepared):
    _, filesystem, release = prepared
    rule = release / "deploy/cloudflared-manager.tmpfiles.conf"
    rule.chmod(0o644)
    rule.write_bytes(b"d /run/cloudflared-manager 0777 root root -\n")
    with pytest.raises(HostOperationError, match="unsafe runtime directory rule"):
        filesystem.install_runtime_tmpfiles(release)


def test_retained_legacy_release_without_runtime_mount_needs_no_new_rule(prepared):
    _, filesystem, release = prepared
    (release / "deploy/cloudflared-manager.tmpfiles.conf").unlink()
    filesystem.validate_deployment_assets(release)
    assert not filesystem.install_runtime_tmpfiles(release)

    unit = release / "deploy/cloudflared-manager.service"
    unit.write_bytes(b"[Service]\nReadWritePaths=/run/cloudflared-manager\n")
    with pytest.raises(HostOperationError, match="lacks its required runtime directory rule"):
        filesystem.validate_deployment_assets(release)
