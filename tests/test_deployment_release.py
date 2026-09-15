import os
from pathlib import Path

import pytest

from cloudflared_manager.deployment.errors import HostOperationError, UpdateLockedError
from cloudflared_manager.deployment.release import DeploymentLock, ReleaseFilesystem
from tests.deployment_support import FakePreparationRunner, make_paths, make_source

OLD_SHA = "1" * 40
NEW_SHA = "2" * 40


def test_release_venv_is_created_at_final_sha_path(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    runner = FakePreparationRunner()
    filesystem = ReleaseFilesystem(paths, owner=None, process_runner=runner)
    filesystem.ensure_layout()
    source = make_source(tmp_path)

    release = filesystem.prepare_release(source, NEW_SHA, Path("/usr/bin/python3"))

    expected_venv = paths.release(NEW_SHA) / ".venv"
    assert release == paths.release(NEW_SHA)
    assert runner.calls[0][0] == [
        "/usr/bin/python3",
        "-I",
        "-m",
        "venv",
        str(expected_venv),
    ]
    pip_arguments = runner.calls[1][0]
    assert pip_arguments[:5] == [
        str(expected_venv / "bin" / "python"),
        "-I",
        "-m",
        "pip",
        "--isolated",
    ]
    assert pip_arguments[pip_arguments.index("--index-url") + 1] == "https://pypi.org/simple"
    assert (release / ".release-ready").read_text().strip() == NEW_SHA
    assert release.stat().st_mode & 0o022 == 0
    assert (expected_venv / "bin" / "cloudflared-manager").stat().st_mode & 0o022 == 0


def test_current_symlink_switch_is_atomic_and_reversible(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    runner = FakePreparationRunner()
    filesystem = ReleaseFilesystem(paths, owner=None, process_runner=runner)
    filesystem.ensure_layout()
    old_source = make_source(tmp_path / "old")
    new_source = make_source(tmp_path / "new")
    filesystem.prepare_release(old_source, OLD_SHA, Path("/usr/bin/python3"))
    filesystem.prepare_release(new_source, NEW_SHA, Path("/usr/bin/python3"))

    assert filesystem.switch_current(OLD_SHA) is None
    previous = filesystem.switch_current(NEW_SHA)
    assert previous == f"releases/{OLD_SHA}"
    assert filesystem.read_current_sha() == NEW_SHA

    filesystem.restore_current(previous)
    assert filesystem.read_current_sha() == OLD_SHA


def test_update_lock_rejects_concurrent_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "update.lock"

    with DeploymentLock(lock_path):
        with pytest.raises(UpdateLockedError):
            with DeploymentLock(lock_path):
                pass


def test_malformed_current_link_is_rejected(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    filesystem = ReleaseFilesystem(paths, owner=None)
    filesystem.ensure_layout()
    paths.current.symlink_to("../../etc/cloudflared")

    with pytest.raises(HostOperationError, match="unsafe"):
        filesystem.read_current_sha()


def test_existing_system_command_directory_mode_is_not_changed(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.update_link.parent.mkdir(parents=True, mode=0o750)
    filesystem = ReleaseFilesystem(paths, owner=None)

    filesystem.ensure_layout()

    assert paths.update_link.parent.stat().st_mode & 0o777 == 0o750


def test_existing_unmarked_install_root_is_rejected(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.install_root.mkdir(parents=True)
    (paths.install_root / "unrelated").write_text("operator data\n", encoding="utf-8")

    with pytest.raises(HostOperationError, match="not recognizable"):
        ReleaseFilesystem(paths, owner=None).ensure_layout()

    assert (paths.install_root / "unrelated").read_text() == "operator data\n"


@pytest.mark.parametrize(
    "collision",
    ["unit", "stable-update", "stable-config", "update-link", "config-link"],
)
def test_unmanaged_deployment_path_collision_is_rejected(
    tmp_path: Path,
    collision: str,
) -> None:
    paths = make_paths(tmp_path)
    filesystem = ReleaseFilesystem(
        paths,
        owner=None,
        process_runner=FakePreparationRunner(),
    )
    filesystem.ensure_layout()
    release = filesystem.prepare_release(
        make_source(tmp_path / "release"),
        NEW_SHA,
        Path("/usr/bin/python3"),
    )
    targets = {
        "unit": paths.unit_path,
        "stable-update": paths.stable_update,
        "stable-config": paths.stable_config,
        "update-link": paths.update_link,
        "config-link": paths.config_link,
    }
    target = targets[collision]
    target.parent.mkdir(parents=True, exist_ok=True)
    if collision.endswith("link"):
        target.symlink_to("/tmp/unrelated-command")
    else:
        target.write_text("unrelated operator content\n", encoding="utf-8")
        target.chmod(0o644)

    with pytest.raises(HostOperationError, match="collides"):
        filesystem.validate_deployment_assets(release)

    if collision.endswith("link"):
        assert target.readlink() == Path("/tmp/unrelated-command")
    else:
        assert target.read_text() == "unrelated operator content\n"


@pytest.mark.parametrize(
    "target_name",
    ["unit", "stable-update", "stable-config", "update-link", "config-link"],
)
def test_first_install_refuses_existing_path_without_active_ownership(
    tmp_path: Path,
    target_name: str,
) -> None:
    paths = make_paths(tmp_path)
    filesystem = ReleaseFilesystem(paths, owner=None)
    filesystem.ensure_layout()
    source = make_source(tmp_path / "candidate")
    targets = {
        "unit": paths.unit_path,
        "stable-update": paths.stable_update,
        "stable-config": paths.stable_config,
        "update-link": paths.update_link,
        "config-link": paths.config_link,
    }
    target = targets[target_name]
    target.parent.mkdir(parents=True, exist_ok=True)
    if target_name.endswith("link"):
        target.symlink_to(
            paths.stable_update if target_name == "update-link" else paths.stable_config
        )
    elif target_name == "unit":
        target.write_bytes((source / "deploy" / "cloudflared-manager.service").read_bytes())
    else:
        script = "update.sh" if target_name == "stable-update" else "config.sh"
        target.write_bytes((source / "deploy" / script).read_bytes())

    with pytest.raises(HostOperationError, match="First installation collides"):
        filesystem.validate_first_install_paths()

    assert target.is_symlink() if target_name.endswith("link") else target.is_file()


def test_reconciliation_rejects_writable_existing_stable_script(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    filesystem = ReleaseFilesystem(
        paths,
        owner=None,
        process_runner=FakePreparationRunner(),
    )
    filesystem.ensure_layout()
    release = filesystem.prepare_release(
        make_source(tmp_path / "release"),
        NEW_SHA,
        Path("/usr/bin/python3"),
    )
    paths.stable_update.write_bytes((release / "deploy" / "update.sh").read_bytes())
    paths.stable_update.chmod(0o777)

    with pytest.raises(HostOperationError, match="unsafe ownership or permissions"):
        filesystem.validate_deployment_assets(release)

    assert paths.stable_update.stat().st_mode & 0o777 == 0o777
