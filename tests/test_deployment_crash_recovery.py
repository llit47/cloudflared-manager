"""Rootless, networkless fault injection for deployment crash recovery."""

import importlib.util
import json
import os
from pathlib import Path

import pytest

from cloudflared_manager.deployment.configurator import Configurator
from cloudflared_manager.deployment.environment import atomic_write_environment, initial_environment
from cloudflared_manager.deployment.errors import (
    EnvironmentFileError, HealthCheckError, HostOperationError, RollbackError,
    TransactionFailedError,
)
from cloudflared_manager.deployment.health import (
    HealthResponse, verify_managed_health, wait_for_readiness,
)
from cloudflared_manager.deployment.installer import Installer
from cloudflared_manager.deployment.release import DEPLOYMENT_MARKER, ReleaseFilesystem
from cloudflared_manager.deployment.settings import ManagerSettings
from cloudflared_manager.deployment.updater import Updater
from cloudflared_manager.runtime_identity import package_release_id
from tests.deployment_support import FakePreparationRunner, FakeService, fake_readiness, make_paths, make_source

A = "a" * 40
B = "b" * 40
PYTHON = Path("/usr/bin/python3")
SETTINGS = ManagerSettings("192.168.1.20", 8000, True)


def installed(tmp_path):
    paths = make_paths(tmp_path)
    fs = ReleaseFilesystem(paths, owner=None, process_runner=FakePreparationRunner())
    fs.ensure_layout()
    for sha in (A, B):
        fs.prepare_release(make_source(tmp_path / sha), sha, PYTHON)
    fs.switch_current(A)
    fs.install_unit(paths.release(A))
    fs.install_stable_administration(paths.release(A))
    atomic_write_environment(paths.environment_file, initial_environment(SETTINGS.bind_host, 8000), owner=None)

    class ProcessService(FakeService):
        release_id = A

        def restart(self):
            super().restart()
            self.release_id = fs.read_current_sha()

        def readiness(self, host, port):
            return fake_readiness(host, port, pid=self.main_pid, release_id=self.release_id)

    return fs, ProcessService()


def test_loaded_package_identity_survives_current_switch(tmp_path):
    from cloudflared_manager import runtime_identity

    package = tmp_path / "releases" / A / ".venv/lib/python3.13/site-packages/cloudflared_manager"
    package.mkdir(parents=True)
    module_file = package / "runtime_identity.py"
    module_file.write_bytes(Path(runtime_identity.__file__).read_bytes())
    current = tmp_path / "current"
    current.symlink_to(f"releases/{A}")
    spec = importlib.util.spec_from_file_location("test_process_identity", module_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    current.unlink()
    current.symlink_to(f"releases/{B}")

    assert module.PROCESS_RELEASE_ID == A
    assert package_release_id(str(module_file)) == A
    assert package_release_id(str(current / ".venv/lib/python3.13/site-packages/cloudflared_manager/runtime_identity.py")) is None


@pytest.mark.parametrize("release", ["current", "bad-sha", "A" * 40, ".."])
def test_ambiguous_package_release_is_not_accepted(release):
    assert package_release_id(
        f"/opt/manager/releases/{release}/.venv/lib/python3.13/site-packages/cloudflared_manager/runtime_identity.py"
    ) is None


def test_current_switch_does_not_change_running_release_readiness(tmp_path):
    fs, service = installed(tmp_path)
    fs.switch_current(B)
    with pytest.raises(HealthCheckError):
        verify_managed_health(service, service.readiness, SETTINGS.bind_host, 8000, SETTINGS.config_id, B)
    service.restart()
    verify_managed_health(service, service.readiness, SETTINGS.bind_host, 8000, SETTINGS.config_id, B)


@pytest.mark.parametrize("restart_works", [True, False])
def test_same_sha_recovers_switched_but_still_running_old_release(tmp_path, monkeypatch, restart_works):
    fs, service = installed(tmp_path)
    fs.switch_current(B)
    if not restart_works:
        monkeypatch.setattr(service, "restart", lambda: service.calls.append("restart"))
        with pytest.raises(RollbackError):
            Updater(fs.paths, fs, service, service.readiness).update(None, B, PYTHON)
        assert service.release_id == A
    else:
        result = Updater(fs.paths, fs, service, service.readiness).update(None, B, PYTHON)
        assert result.changed
        assert service.release_id == B
    assert "restart" in service.calls


@pytest.mark.parametrize("transaction", ["update", "config"])
def test_rollback_rejects_wrong_executing_release(tmp_path, transaction):
    fs, service = installed(tmp_path)
    calls = 0

    def health(host, port):
        nonlocal calls
        calls += 1
        if calls == 1:
            return service.readiness(host, port)
        if calls == 2:
            raise HealthCheckError("candidate failed")
        return fake_readiness(host, port, release_id=B)

    with pytest.raises(RollbackError):
        if transaction == "update":
            Updater(fs.paths, fs, service, health).update(make_source(tmp_path / "candidate"), B, PYTHON)
        else:
            Configurator(fs.paths, service, health, environment_owner=None).apply({"CFM_BIND_PORT": "9000"})
    assert fs.read_current_sha() == A
    assert fs.paths.environment_file.read_bytes() == initial_environment(SETTINGS.bind_host, 8000).render().encode()


def test_config_refuses_to_adopt_pending_current_release(tmp_path):
    fs, service = installed(tmp_path)
    fs.switch_current(B)
    before = fs.paths.environment_file.read_bytes()
    with pytest.raises(HealthCheckError):
        Configurator(fs.paths, service, service.readiness, environment_owner=None).apply({"CFM_BIND_PORT": "9000"})
    assert "restart" not in service.calls
    assert service.release_id == A
    assert fs.paths.environment_file.read_bytes() == before


@pytest.mark.parametrize("identity", [None, "", "bad", "A" * 40, "a" * 39, 123])
def test_readiness_http_rejects_invalid_release_identity(identity):
    payload = {"status": "ready", "app": "cloudflared-manager", "pid": 1234,
               "config_id": SETTINGS.config_id, "release_id": identity}
    with pytest.raises(HealthCheckError):
        wait_for_readiness(SETTINGS.bind_host, 8000, attempts=1,
                           fetcher=lambda url, timeout: HealthResponse(200, json.dumps(payload).encode()))


def test_readiness_http_accepts_exact_release_identity():
    payload = {"status": "ready", "app": "cloudflared-manager", "pid": 1234,
               "config_id": SETTINGS.config_id, "release_id": A}
    result = wait_for_readiness(SETTINGS.bind_host, 8000, attempts=1,
                               fetcher=lambda url, timeout: HealthResponse(200, json.dumps(payload).encode()))
    assert result.release_id == A


@pytest.mark.parametrize("stage", ["before-marker", "after-marker", "staged-sync", "before-publish", "after-publish", "parent-sync"])
def test_interrupted_root_publication_is_retryable(tmp_path, monkeypatch, stage):
    paths = make_paths(tmp_path)
    fs = ReleaseFilesystem(paths, owner=None)
    write, publish, sync = fs.atomic_write, fs._publish_install_root, fs._sync_directory

    def interrupted_write(path, content, mode):
        if stage == "before-marker":
            raise OSError("interrupted")
        write(path, content, mode)
        if stage == "after-marker":
            raise OSError("interrupted")

    def interrupted_publish(temporary, root):
        assert (temporary / DEPLOYMENT_MARKER).is_file()
        assert not root.exists()
        if stage == "before-publish":
            raise OSError("interrupted")
        publish(temporary, root)
        if stage == "after-publish":
            raise OSError("interrupted")

    def interrupted_sync(path):
        if (stage == "parent-sync" and path == paths.install_root.parent) or (
            stage == "staged-sync" and path.name.startswith(".cloudflared-manager.")
        ):
            raise OSError("interrupted")
        sync(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(fs, "atomic_write", interrupted_write)
        scoped.setattr(fs, "_publish_install_root", interrupted_publish)
        scoped.setattr(fs, "_sync_directory", interrupted_sync)
        with pytest.raises(HostOperationError):
            fs.ensure_layout()
    if paths.install_root.exists():
        fs.require_owned_layout()
    fs.ensure_layout()
    fs.require_owned_layout()
    assert paths.releases.is_dir()


@pytest.mark.parametrize("collision", ["empty-directory", "file", "symlink"])
def test_atomic_root_publication_never_replaces_competing_object(tmp_path, monkeypatch, collision):
    paths = make_paths(tmp_path)
    fs = ReleaseFilesystem(paths, owner=None)
    publish = fs._publish_install_root
    external = tmp_path / "external"
    external.mkdir()

    def collision_wins(temporary, root):
        if collision == "empty-directory":
            root.mkdir()
        elif collision == "file":
            root.write_bytes(b"foreign")
        else:
            root.symlink_to(external)
        publish(temporary, root)

    monkeypatch.setattr(fs, "_publish_install_root", collision_wins)
    with pytest.raises(HostOperationError):
        fs.ensure_layout()
    assert not (external / DEPLOYMENT_MARKER).exists()
    if collision == "file":
        assert paths.install_root.read_bytes() == b"foreign"
    else:
        assert not (paths.install_root / DEPLOYMENT_MARKER).exists()
    with pytest.raises(HostOperationError):
        fs.ensure_layout()


@pytest.mark.parametrize("kind", ["regular", "symlink", "directory", "writable"])
def test_fresh_install_rejects_foreign_environment_without_mutation(tmp_path, kind):
    paths = make_paths(tmp_path)
    paths.config_root.mkdir(parents=True)
    external = tmp_path / "foreign"
    content = b"# foreign\nUNKNOWN_SETTING=untrusted\n"
    external.write_bytes(content)
    if kind == "symlink":
        paths.environment_file.symlink_to(external)
    elif kind == "directory":
        paths.environment_file.mkdir()
    else:
        paths.environment_file.write_bytes(content)
        paths.environment_file.chmod(0o666 if kind == "writable" else 0o600)
    before = paths.environment_file.lstat()
    fs = ReleaseFilesystem(paths, owner=None, process_runner=FakePreparationRunner())
    service = FakeService(active=False)
    identities = []
    with pytest.raises(HostOperationError, match="unowned environment"):
        Installer(paths, fs, service, fake_readiness,
                  lambda: identities.append(True), environment_owner=None).install(
            make_source(tmp_path / "candidate"), A, PYTHON, SETTINGS.bind_host, 8000)
    after = paths.environment_file.lstat()
    assert (after.st_ino, after.st_mode, after.st_uid, after.st_gid) == (before.st_ino, before.st_mode, before.st_uid, before.st_gid)
    assert external.read_bytes() == content
    if kind in {"regular", "writable"}:
        assert paths.environment_file.read_bytes() == content
    assert not paths.install_root.exists()
    assert not service.calls and not identities


def test_owned_interrupted_install_preserves_safe_environment(tmp_path):
    paths = make_paths(tmp_path)
    fs = ReleaseFilesystem(paths, owner=None, process_runner=FakePreparationRunner())
    fs.ensure_layout()
    source = make_source(tmp_path / "candidate")
    fs.prepare_release(source, A, PYTHON)
    content = b"# owned\nUNKNOWN_SETTING=preserved\n" + initial_environment(SETTINGS.bind_host, 8000).render().encode()
    atomic_write_environment(paths.environment_file, content, owner=None)
    result = Installer(paths, fs, FakeService(active=False),
                       lambda host, port: fake_readiness(host, port, release_id=A),
                       lambda: None, environment_owner=None).install(source, A, PYTHON, SETTINGS.bind_host, 8000)
    assert result.sha == A
    assert paths.environment_file.read_bytes() == content


def test_ownership_marker_alone_cannot_adopt_preexisting_environment(tmp_path):
    paths = make_paths(tmp_path)
    fs = ReleaseFilesystem(paths, owner=None)
    fs.ensure_layout()
    content = b"UNKNOWN_SETTING=foreign\n"
    atomic_write_environment(paths.environment_file, content, owner=None)
    with pytest.raises(HostOperationError, match="no prepared manager release"):
        fs.validate_first_install_paths(A)
    assert paths.environment_file.read_bytes() == content


@pytest.mark.parametrize("unsafe", ["file-mode", "file-owner", "parent-mode", "parent-symlink"])
def test_marked_install_cannot_repair_unsafe_configuration_into_ownership(tmp_path, monkeypatch, unsafe):
    fs, _ = installed(tmp_path)
    fs.paths.current.unlink()
    for path in (fs.paths.stable_update, fs.paths.stable_config, fs.paths.update_link, fs.paths.config_link):
        path.unlink()
    env = fs.paths.environment_file
    before = env.read_bytes()
    if unsafe == "file-mode":
        env.chmod(0o666)
    elif unsafe == "parent-mode":
        env.parent.chmod(0o777)
    elif unsafe == "parent-symlink":
        external = tmp_path / "external-config"
        env.parent.rename(external)
        env.parent.symlink_to(external)
    else:
        fs.owner = (os.getuid(), os.getgid())
        original = Path.lstat

        def wrong_owner(path, *args, **kwargs):
            metadata = original(path, *args, **kwargs)
            if path == env:
                values = list(metadata)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return metadata

        monkeypatch.setattr(Path, "lstat", wrong_owner)
    metadata = env.lstat()
    with pytest.raises(EnvironmentFileError):
        fs.validate_first_install_paths(A)
    assert env.read_bytes() == before
    assert env.lstat() == metadata


def test_package_identity_rejects_symlinked_immutable_location(tmp_path):
    release = tmp_path / "releases" / A
    package = release / ".venv/lib/python3.13/site-packages/cloudflared_manager"
    package.mkdir(parents=True)
    source = package / "runtime_identity.py"
    source.write_text("# fixture\n")
    other = tmp_path / "releases" / B
    other.symlink_to(release)
    assert package_release_id(str(source)) == A
    assert package_release_id(str(other / source.relative_to(release))) is None


@pytest.mark.parametrize("unsafe", ["writable", "wrong-owner"])
def test_empty_foreign_configuration_directory_is_never_repaired(tmp_path, monkeypatch, unsafe):
    paths = make_paths(tmp_path)
    paths.config_root.mkdir(parents=True)
    fs = ReleaseFilesystem(paths, owner=None)
    if unsafe == "writable":
        paths.config_root.chmod(0o777)
    else:
        fs.owner = (os.getuid(), os.getgid())
        # Set up the owned install root before simulating a foreign config owner.
        fs._ensure_install_root()
        original = Path.lstat

        def foreign_owner(path, *args, **kwargs):
            metadata = original(path, *args, **kwargs)
            if path == paths.config_root:
                values = list(metadata)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return metadata

        monkeypatch.setattr(Path, "lstat", foreign_owner)
    before = paths.config_root.lstat()
    with pytest.raises(HostOperationError, match="directory is unsafe"):
        fs.ensure_layout()
    after = paths.config_root.lstat()
    assert (before.st_mode, before.st_uid, before.st_gid) == (after.st_mode, after.st_uid, after.st_gid)
    assert not paths.environment_file.exists()


@pytest.mark.parametrize("removed", ["unit", "environment"])
@pytest.mark.parametrize("sync_fails", [False, True])
def test_first_install_rollback_deletions_are_durable_or_report_failure(
    tmp_path, monkeypatch, removed, sync_fails
):
    paths = make_paths(tmp_path)
    fs = ReleaseFilesystem(paths, owner=None, process_runner=FakePreparationRunner())
    target = paths.unit_path if removed == "unit" else paths.environment_file
    original = fs._sync_directory
    synced = []
    rollback_started = False

    def sync(path):
        if rollback_started and path == target.parent and not target.exists():
            synced.append(path)
            if sync_fails:
                raise OSError("synthetic directory sync failure")
        original(path)

    def unhealthy(host, port):
        nonlocal rollback_started
        rollback_started = True
        raise HealthCheckError("candidate unhealthy")

    monkeypatch.setattr(fs, "_sync_directory", sync)
    error = RollbackError if sync_fails else TransactionFailedError
    with pytest.raises(error):
        Installer(paths, fs, FakeService(active=False), unhealthy, lambda: None,
                  environment_owner=None).install(
            make_source(tmp_path / "candidate"), A, PYTHON, SETTINGS.bind_host, 8000)
    assert synced == [target.parent]
    assert not target.exists()
