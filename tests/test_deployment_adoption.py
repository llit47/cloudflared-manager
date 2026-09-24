from __future__ import annotations

import os
from pathlib import Path

import pytest

from cloudflared_manager.cloudflared import CloudflaredRuntime, ManagementMode
from cloudflared_manager.deployment.adoption import (
    CloudflaredConfigAdopter,
    _require_manager_service_sandbox_visible,
)
from cloudflared_manager.deployment.configurator import Configurator
from cloudflared_manager.deployment.environment import (
    atomic_write_environment,
    initial_environment,
    read_environment,
)
from cloudflared_manager.deployment.errors import (
    HealthCheckError,
    HostOperationError,
    TransactionFailedError,
    ValidationError,
)
from cloudflared_manager.deployment.health import DeploymentReadiness
from cloudflared_manager.deployment.release import ReleaseFilesystem
from cloudflared_manager.deployment.settings import settings_from_document
from cloudflared_manager.runtime_identity import runtime_config_id
from tests.deployment_support import (
    FakePreparationRunner,
    FakeService,
    make_paths,
    make_source,
)

SHA = "7" * 40
VALID_CONFIG = """\
tunnel: test-tunnel
credentials-file: /root/must-not-be-read.json
ingress:
  - hostname: app.example.test
    service: http://127.0.0.1:8080
  - service: http_status:404
"""


def test_adoption_rejects_unsafe_writable_cloudflared_tree_before_persist(tmp_path, monkeypatch):
    from cloudflared_manager.deployment import adoption
    candidate = _config_file(tmp_path)
    _, service, adopter = _adopter(tmp_path, candidate)

    def reject(path, *, service_uid):
        assert path == candidate
        raise HostOperationError("unsafe cloudflared tree")

    monkeypatch.setattr(adoption, "require_adopted_write_boundary", reject)
    with pytest.raises(HostOperationError, match="unsafe cloudflared tree"):
        adopter.adopt_detected()
    assert service.calls == []


def _runtime(
    path: Path | None,
    *,
    mode: ManagementMode = ManagementMode.LOCAL_CONFIG,
    systemd_available: bool = True,
    service_exists: bool | None = True,
) -> CloudflaredRuntime:
    return CloudflaredRuntime(
        executable_path=Path("/usr/bin/cloudflared"),
        version="2026.9.1",
        systemd_available=systemd_available,
        service_exists=service_exists,
        load_state="loaded" if service_exists else "not-found",
        active_state="active",
        sub_state="running",
        enabled_state="enabled",
        main_pid=4321,
        management_mode=mode,
        explicit_config_path=path,
    )


def _installed(tmp_path: Path):
    paths = make_paths(tmp_path)
    filesystem = ReleaseFilesystem(
        paths,
        owner=None,
        process_runner=FakePreparationRunner(),
    )
    filesystem.ensure_layout()
    release = filesystem.prepare_release(
        make_source(tmp_path / "release"),
        SHA,
        Path("/usr/bin/python3"),
    )
    filesystem.switch_current(SHA)
    filesystem.install_unit(release)
    filesystem.install_stable_administration(release)
    atomic_write_environment(
        paths.environment_file,
        initial_environment("192.168.1.20", 8000),
        owner=None,
    )
    service = FakeService()
    service.release_id = SHA
    return paths, service


def _config_file(tmp_path: Path, contents: str = VALID_CONFIG) -> Path:
    path = tmp_path / "cloudflared" / "config.yml"
    path.parent.mkdir(mode=0o755)
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o644)
    return path


def _adopter(tmp_path: Path, candidate: Path, health=None):
    paths, service = _installed(tmp_path)

    def current_readiness(host: str, port: int) -> DeploymentReadiness:
        settings = settings_from_document(read_environment(paths.environment_file)[0])
        return DeploymentReadiness(service.main_pid, settings.config_id, SHA)

    configurator = Configurator(
        paths,
        service,
        health or current_readiness,
        environment_owner=None,
    )
    adopter = CloudflaredConfigAdopter(
        configurator,
        runtime_discovery=lambda enabled: _runtime(candidate),
        identity_provider=lambda: (os.getuid(), os.getgid()),
        sandbox_path_validator=lambda path: None,
    )
    return paths, service, adopter


def test_successful_adoption_persists_path_restarts_once_and_extends_identity(
    tmp_path: Path,
) -> None:
    candidate = _config_file(tmp_path)
    paths, service, adopter = _adopter(tmp_path, candidate)
    original = paths.environment_file.read_text(encoding="utf-8")
    paths.environment_file.write_text(
        "# operator note\nFUTURE_SETTING=keep\n" + original,
        encoding="utf-8",
    )

    result = adopter.adopt_detected()

    rendered = paths.environment_file.read_text(encoding="utf-8")
    assert result.changed is True
    assert result.settings.cloudflared_config_path == candidate
    assert result.settings.config_id == runtime_config_id(
        "192.168.1.20", 8000, True, candidate
    )
    assert f"CFM_CLOUDFLARED_CONFIG_PATH={candidate}\n" in rendered
    assert "# operator note\nFUTURE_SETTING=keep\n" in rendered
    assert service.calls == ["restart"]


def test_repeated_adoption_is_healthy_noop_without_restart(tmp_path: Path) -> None:
    candidate = _config_file(tmp_path)
    _, service, adopter = _adopter(tmp_path, candidate)
    adopter.adopt_detected()
    service.calls.clear()

    result = adopter.adopt_detected()

    assert result.changed is False
    assert service.calls == []


def test_clear_removes_path_and_restarts_once_then_becomes_noop(tmp_path: Path) -> None:
    candidate = _config_file(tmp_path)
    paths, service, adopter = _adopter(tmp_path, candidate)
    adopter.adopt_detected()
    service.calls.clear()

    changed = adopter.clear()

    assert changed.changed is True
    assert "CFM_CLOUDFLARED_CONFIG_PATH" not in paths.environment_file.read_text()
    assert service.calls == ["restart"]
    service.calls.clear()

    unchanged = adopter.clear()

    assert unchanged.changed is False
    assert service.calls == []


def test_failed_adoption_health_restores_exact_file_and_verifies_previous_identity(
    tmp_path: Path,
) -> None:
    candidate = _config_file(tmp_path)
    paths, service = _installed(tmp_path)
    previous = b"# exact previous\nFUTURE=opaque\n" + paths.environment_file.read_bytes()
    paths.environment_file.write_bytes(previous)
    health_calls = 0

    def health(host: str, port: int) -> DeploymentReadiness:
        nonlocal health_calls
        health_calls += 1
        if health_calls == 1:
            raise HealthCheckError("candidate failed")
        return DeploymentReadiness(
            service.main_pid,
            runtime_config_id(host, port, True),
            SHA,
        )

    adopter = CloudflaredConfigAdopter(
        Configurator(paths, service, health, environment_owner=None),
        runtime_discovery=lambda enabled: _runtime(candidate),
        identity_provider=lambda: (os.getuid(), os.getgid()),
        sandbox_path_validator=lambda path: None,
    )

    with pytest.raises(TransactionFailedError, match="restored"):
        adopter.adopt_detected()

    assert paths.environment_file.read_bytes() == previous
    assert service.calls == ["restart", "restart"]
    assert health_calls == 2


@pytest.mark.parametrize("mode", [ManagementMode.REMOTE_TOKEN, ManagementMode.UNKNOWN])
def test_non_local_management_mode_cannot_be_adopted(
    tmp_path: Path,
    mode: ManagementMode,
) -> None:
    candidate = _config_file(tmp_path)
    paths, service, adopter = _adopter(tmp_path, candidate)
    adopter.runtime_discovery = lambda enabled: _runtime(candidate, mode=mode)
    previous = paths.environment_file.read_bytes()

    with pytest.raises(HostOperationError, match="not in local-config mode"):
        adopter.adopt_detected()

    assert paths.environment_file.read_bytes() == previous
    assert service.calls == []


def test_missing_explicit_detected_path_cannot_be_adopted(tmp_path: Path) -> None:
    candidate = _config_file(tmp_path)
    paths, service, adopter = _adopter(tmp_path, candidate)
    adopter.runtime_discovery = lambda enabled: _runtime(None)

    with pytest.raises(HostOperationError, match="no explicit config path"):
        adopter.adopt_detected()

    assert "CFM_CLOUDFLARED_CONFIG_PATH" not in paths.environment_file.read_text()
    assert service.calls == []


def test_unavailable_runtime_discovery_cannot_be_adopted(tmp_path: Path) -> None:
    candidate = _config_file(tmp_path)
    paths, service, adopter = _adopter(tmp_path, candidate)
    adopter.runtime_discovery = lambda enabled: _runtime(
        candidate,
        systemd_available=False,
        service_exists=None,
    )

    with pytest.raises(HostOperationError, match="did not find"):
        adopter.adopt_detected()

    assert "CFM_CLOUDFLARED_CONFIG_PATH" not in paths.environment_file.read_text()
    assert service.calls == []


def test_disabled_runtime_discovery_cannot_be_adopted(tmp_path: Path) -> None:
    candidate = _config_file(tmp_path)
    paths, service, adopter = _adopter(tmp_path, candidate)
    document, _ = read_environment(paths.environment_file)
    atomic_write_environment(
        paths.environment_file,
        document.updated({"CFM_RUNTIME_DISCOVERY_ENABLED": "false"}),
        owner=None,
    )
    calls: list[bool] = []
    adopter.runtime_discovery = lambda enabled: (calls.append(enabled) or _runtime(candidate))

    with pytest.raises(HostOperationError, match="must be enabled"):
        adopter.adopt_detected()

    assert calls == []
    assert service.calls == []


@pytest.mark.parametrize(
    ("candidate_factory", "message"),
    [
        (lambda root: root / "missing.yml", "does not exist"),
        (lambda root: root / "cloudflared" / "../config.yml", "canonical"),
    ],
)
def test_missing_or_noncanonical_candidate_fails_closed(
    tmp_path: Path,
    candidate_factory,
    message: str,
) -> None:
    candidate = candidate_factory(tmp_path)
    paths, service, adopter = _adopter(tmp_path, candidate)

    with pytest.raises(ValidationError, match=message):
        adopter.adopt_detected()

    assert "CFM_CLOUDFLARED_CONFIG_PATH" not in paths.environment_file.read_text()
    assert service.calls == []


def test_final_symlink_candidate_is_rejected(tmp_path: Path) -> None:
    target = _config_file(tmp_path)
    candidate = tmp_path / "cloudflared" / "linked.yml"
    candidate.symlink_to(target)
    paths, service, adopter = _adopter(tmp_path, candidate)

    with pytest.raises(ValidationError, match="not canonical"):
        adopter.adopt_detected()

    assert "CFM_CLOUDFLARED_CONFIG_PATH" not in paths.environment_file.read_text()
    assert service.calls == []


@pytest.mark.parametrize(
    "contents",
    [
        "ingress: not-a-list\n",
        "credentials-file: /root/must-not-be-read.json\ningress: [\n",
    ],
)
def test_invalid_yaml_or_structure_is_rejected_before_environment_change(
    tmp_path: Path,
    contents: str,
) -> None:
    candidate = _config_file(tmp_path, contents)
    paths, service, adopter = _adopter(tmp_path, candidate)
    previous = paths.environment_file.read_bytes()

    with pytest.raises(ValidationError, match="not valid for adoption"):
        adopter.adopt_detected()

    assert paths.environment_file.read_bytes() == previous
    assert service.calls == []


@pytest.mark.parametrize(
    "candidate",
    [
        Path("relative/config.yml"),
        Path("/etc/cloudflared/config.json"),
        Path("/etc/cloudflared/../secret.yml"),
    ],
)
def test_unsafe_detected_path_is_rejected_before_environment_change(
    tmp_path: Path,
    candidate: Path,
) -> None:
    paths, service, adopter = _adopter(tmp_path, candidate)

    with pytest.raises(ValidationError, match="safe canonical"):
        adopter.adopt_detected()

    assert "CFM_CLOUDFLARED_CONFIG_PATH" not in paths.environment_file.read_text()
    assert service.calls == []


def test_file_unreadable_by_service_identity_is_rejected(tmp_path: Path) -> None:
    candidate = _config_file(tmp_path)
    candidate.chmod(0o600)
    paths, service, adopter = _adopter(tmp_path, candidate)
    adopter.identity_provider = lambda: (candidate.stat().st_uid + 1, candidate.stat().st_gid + 1)

    with pytest.raises(ValidationError, match="not readable"):
        adopter.adopt_detected()

    assert "CFM_CLOUDFLARED_CONFIG_PATH" not in paths.environment_file.read_text()
    assert service.calls == []


@pytest.mark.parametrize(
    "candidate",
    [
        Path("/root/cloudflared/config.yml"),
        Path("/home/operator/cloudflared/config.yml"),
        Path("/run/user/1000/cloudflared/config.yml"),
        Path("/tmp/cloudflared/config.yml"),
        Path("/var/tmp/cloudflared/config.yml"),
    ],
)
def test_service_sandbox_hidden_path_is_rejected_before_mutation_or_restart(
    tmp_path: Path,
    candidate: Path,
) -> None:
    paths, service = _installed(tmp_path)
    previous = paths.environment_file.read_bytes()

    def unexpected_identity() -> tuple[int, int]:
        raise AssertionError("sandbox rejection must precede identity lookup")

    def unexpected_parser(path: Path):
        raise AssertionError("sandbox rejection must precede config parsing")

    adopter = CloudflaredConfigAdopter(
        Configurator(paths, service, lambda host, port: None, environment_owner=None),
        runtime_discovery=lambda enabled: _runtime(candidate),
        config_parser=unexpected_parser,
        identity_provider=unexpected_identity,
    )

    with pytest.raises(ValidationError, match="service sandbox"):
        adopter.adopt_detected()

    assert paths.environment_file.read_bytes() == previous
    assert service.calls == []


@pytest.mark.parametrize(
    "candidate",
    [
        Path("/root"),
        Path("/home"),
        Path("/run/user"),
        Path("/tmp"),
        Path("/var/tmp"),
    ],
)
def test_service_sandbox_rejects_exact_hidden_prefix(candidate: Path) -> None:
    with pytest.raises(ValidationError, match="service sandbox"):
        _require_manager_service_sandbox_visible(candidate)


@pytest.mark.parametrize(
    "candidate",
    [
        Path("/rooted/cloudflared/config.yml"),
        Path("/home2/cloudflared/config.yml"),
        Path("/run/userland/cloudflared/config.yml"),
        Path("/tmp2/cloudflared/config.yml"),
        Path("/var/tmp2/cloudflared/config.yml"),
        Path("/etc/cloudflared/config.yml"),
    ],
)
def test_service_sandbox_prefix_boundaries_do_not_reject_unrelated_paths(
    candidate: Path,
) -> None:
    _require_manager_service_sandbox_visible(candidate)


def test_legacy_no_path_runtime_identity_is_exactly_preserved() -> None:
    assert runtime_config_id("192.168.1.20", 8000, True, None) == (
        "98d70920f73d25c1a7d6d231a76d272373ebf03f1bdb373d2527278ecc73746c"
    )


def test_existing_environment_without_optional_path_remains_valid() -> None:
    settings = settings_from_document(initial_environment("192.168.1.20", 8000))

    assert settings.cloudflared_config_path is None
    assert settings.config_id == runtime_config_id("192.168.1.20", 8000, True)
