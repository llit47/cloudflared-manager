"""Disposable DAC proof for paths made writable in the manager mount view."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloudflared_manager.deployment.errors import HostOperationError
from cloudflared_manager.deployment.write_boundary import (
    require_adopted_write_boundary, require_nonprivate_adoption,
    require_write_boundary,
)
from tests.deployment_support import make_paths


@pytest.fixture
def boundary(tmp_path):
    tmp_path.chmod(0o700)
    paths = make_paths(tmp_path)
    paths.config_root.mkdir(parents=True)
    paths.config_root.parent.chmod(0o755)
    paths.config_root.chmod(0o750)
    paths.environment_file.write_text("CFM_MODE=production\n")
    paths.environment_file.chmod(0o600)
    paths.runtime_root.mkdir(parents=True)
    paths.runtime_root.parent.chmod(0o755)
    paths.runtime_root.chmod(0o700)
    cloudflared = tmp_path / "etc/cloudflared"
    cloudflared.mkdir()
    cloudflared.chmod(0o755)
    config = cloudflared / "config.yml"
    config.write_text("ingress: []\n")
    config.chmod(0o644)
    credential = cloudflared / "credentials.json"
    credential.write_text("{}\n")
    credential.chmod(0o600)
    return paths, cloudflared, config, credential, tmp_path


def check(boundary, **overrides):
    paths, cloudflared, config, _, root = boundary
    options = dict(adopted=config, cloudflared_root=cloudflared,
                   trusted_uid=os.getuid(), service_uid=os.getuid() + 1,
                   anchor=root)
    options.update(overrides)
    require_write_boundary(paths, **options)


def test_secure_tree_retains_root_recovery_write_authority(boundary):
    paths, cloudflared, config, _, root = boundary
    check(boundary)
    # The root transaction still has a writable parent and can stage a file.
    candidate = cloudflared / ".cfm-test-candidate"
    candidate.write_text("candidate\n")
    candidate.unlink()
    require_adopted_write_boundary(config, service_uid=os.getuid() + 1,
                                   cloudflared_root=cloudflared,
                                   trusted_uid=os.getuid(), anchor=root)


@pytest.mark.parametrize("target,mode", [
    ("config", 0o666), ("cloudflared", 0o777),
    ("credential", 0o666), ("config_root", 0o770),
    ("runtime_root", 0o770),
])
def test_writable_privileged_object_fails_closed(boundary, target, mode):
    paths, cloudflared, config, credential, _ = boundary
    object_path = {
        "config": config, "cloudflared": cloudflared,
        "credential": credential, "config_root": paths.config_root,
        "runtime_root": paths.runtime_root,
    }[target]
    object_path.chmod(mode)
    with pytest.raises(HostOperationError):
        check(boundary)


@pytest.mark.parametrize("target,attribute", [
    ("cloudflared", "system.posix_acl_default"),
    ("cloudflared", "system.posix_acl_access"),
    ("cloudflared", "system.nfs4_acl"),
    ("cloudflared", "system.richacl"),
    ("cloudflared", "system.future_acl"),
    ("nested", "system.posix_acl_default"),
    ("config_root", "system.posix_acl_default"),
    ("runtime_root", "system.posix_acl_default"),
])
def test_inheritable_and_other_system_acls_block_write_boundary(
    boundary, monkeypatch, target, attribute,
):
    from cloudflared_manager.deployment import write_boundary
    paths, cloudflared, config, _, root = boundary
    nested = cloudflared / "nested"
    nested.mkdir()
    target_path = {
        "cloudflared": cloudflared, "nested": nested,
        "config_root": paths.config_root, "runtime_root": paths.runtime_root,
    }[target]
    original_listxattr = write_boundary.os.listxattr

    def listxattr(path, *, follow_symlinks=True):
        if Path(path) == target_path:
            return [attribute]
        return original_listxattr(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(write_boundary.os, "listxattr", listxattr)
    with pytest.raises(HostOperationError):
        check(boundary)
    assert not (cloudflared / ".cfm-test-candidate").exists()
    if target in {"cloudflared", "nested"}:
        with pytest.raises(HostOperationError):
            require_adopted_write_boundary(config, service_uid=os.getuid() + 1,
                                           cloudflared_root=cloudflared,
                                           trusted_uid=os.getuid(), anchor=root)


def test_web_owned_adopted_file_fails_even_when_mode_has_no_write_bits(boundary):
    from cloudflared_manager.deployment.write_boundary import _no_acl_or_write
    _, _, config, _, _ = boundary
    config.chmod(0o400)
    with pytest.raises(HostOperationError, match="ownership"):
        _no_acl_or_write(config, config.stat(), os.getuid() + 1, os.getuid(),
                         False, require_root_owner=False)


def test_manager_private_paths_cannot_be_adopted(boundary):
    paths, _, _, _, _ = boundary
    for candidate in (paths.config_root / "config.yml",
                      paths.runtime_root / "config.yml"):
        with pytest.raises(HostOperationError):
            require_nonprivate_adoption(candidate, paths)


def test_symlink_and_oversized_tree_fail_closed(boundary):
    _, cloudflared, _, _, _ = boundary
    (cloudflared / "other.yml").symlink_to("config.yml")
    with pytest.raises(HostOperationError):
        check(boundary)


def test_runtime_probe_rejects_effective_write_access_even_with_safe_mode(boundary, monkeypatch):
    from cloudflared_manager.deployment import write_boundary
    _, _, config, _, _ = boundary
    service_uid = os.getuid() + 1
    monkeypatch.setattr(write_boundary.os, "geteuid", lambda: service_uid)
    original_access = write_boundary.os.access
    checked = []
    def access(path, mode):
        if mode == os.W_OK:
            checked.append(Path(path))
            return Path(path) == config
        return original_access(path, mode)
    monkeypatch.setattr(write_boundary.os, "access",
                        access)
    with pytest.raises(HostOperationError):
        check(boundary, probe_as_service=True)
    assert config in checked


def test_runtime_probe_accepts_root_private_state_and_readonly_cloudflared_tree(boundary, monkeypatch):
    from cloudflared_manager.deployment import write_boundary
    monkeypatch.setattr(write_boundary.os, "geteuid", lambda: os.getuid() + 1)
    original_access = write_boundary.os.access
    monkeypatch.setattr(write_boundary.os, "access",
                        lambda path, mode: False if mode == os.W_OK
                        else original_access(path, mode))
    check(boundary, probe_as_service=True)


def test_runtime_probe_allows_cloudflared_tree_inaccessible_to_web_uid(boundary, monkeypatch):
    from cloudflared_manager.deployment import write_boundary
    _, cloudflared, _, _, _ = boundary
    cloudflared.chmod(0o700)
    monkeypatch.setattr(write_boundary.os, "geteuid", lambda: os.getuid() + 1)
    original_access = write_boundary.os.access
    monkeypatch.setattr(write_boundary.os, "access",
                        lambda path, mode: False if mode == os.W_OK
                        or (Path(path) == cloudflared and mode == os.X_OK)
                        else original_access(path, mode))
    check(boundary, probe_as_service=True)


def test_production_entrypoint_rejects_unsafe_boundary_before_serving(monkeypatch):
    import uvicorn
    from cloudflared_manager import main
    from cloudflared_manager.deployment import write_boundary

    monkeypatch.setattr(main.Settings, "from_env", lambda: SimpleNamespace(
        mode="production", cloudflared_config_path=Path("/etc/cloudflared/config.yml"),
        bind_host="127.0.0.1", bind_port=8000,
    ))
    monkeypatch.setattr(write_boundary, "require_write_boundary",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            HostOperationError("unsafe write boundary")))
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: pytest.fail(
        "server started without a safe write boundary"))
    with pytest.raises(HostOperationError, match="unsafe write boundary"):
        main.run()
