import os
from pathlib import Path

import pytest

from cloudflared_manager.deployment.environment import (
    EnvironmentDocument,
    atomic_write_environment,
    initial_environment,
    read_environment,
)
from cloudflared_manager.deployment.errors import EnvironmentFileError


def test_initial_environment_contains_only_read_only_release_settings() -> None:
    rendered = initial_environment("192.168.1.10", 8000).render()

    assert "CFM_MODE=production" in rendered
    assert "CFM_RUNTIME_DISCOVERY_ENABLED=true" in rendered
    assert "CFM_CLOUDFLARED_CONFIG_PATH" not in rendered
    assert "TOKEN" not in rendered


def test_atomic_environment_creation_has_private_mode(tmp_path: Path) -> None:
    path = tmp_path / "cloudflared-manager.env"

    atomic_write_environment(
        path,
        initial_environment("10.0.0.2", 8000),
        owner=None,
    )

    document, _ = read_environment(path)
    assert document.managed_values()["CFM_BIND_HOST"] == "10.0.0.2"
    assert path.stat().st_mode & 0o777 == 0o600


def test_environment_update_preserves_comments_unknown_keys_and_order() -> None:
    document = EnvironmentDocument.parse(
        "# operator note\nFUTURE_SETTING=keep-me\nCFM_BIND_PORT=8000\n# tail\n"
    )

    updated = document.updated({"CFM_BIND_PORT": "9000"}).render()

    assert updated == (
        "# operator note\nFUTURE_SETTING=keep-me\nCFM_BIND_PORT=9000\n# tail\n"
    )


def test_optional_config_path_can_be_added_and_removed_without_touching_unknown_data() -> None:
    document = EnvironmentDocument.parse(
        "# operator note\nFUTURE_SETTING=keep-me\nCFM_BIND_PORT=8000\n# tail\n"
    )

    adopted = document.updated(
        {"CFM_CLOUDFLARED_CONFIG_PATH": "/etc/cloudflared/config.yml"}
    )
    cleared = adopted.updated({"CFM_CLOUDFLARED_CONFIG_PATH": None})

    assert adopted.render() == (
        "# operator note\nFUTURE_SETTING=keep-me\nCFM_BIND_PORT=8000\n# tail\n\n"
        "CFM_CLOUDFLARED_CONFIG_PATH=/etc/cloudflared/config.yml\n"
    )
    assert cleared.render() == document.render()


@pytest.mark.parametrize(
    "value",
    [
        "relative/config.yml",
        "/etc/cloudflared/../secret.yml",
        "/etc/cloudflared/config with spaces.yml",
        "/etc/cloudflared/config.json",
    ],
)
def test_optional_config_path_uses_key_specific_strict_validation(value: str) -> None:
    document = EnvironmentDocument.parse("CFM_BIND_PORT=8000\n")

    with pytest.raises(EnvironmentFileError, match="path is not safe"):
        document.updated({"CFM_CLOUDFLARED_CONFIG_PATH": value})


def test_environment_data_is_never_executed(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    document = EnvironmentDocument.parse(
        f"FUTURE_SETTING=$(touch {marker})\nCFM_BIND_PORT=8000\n"
    )

    assert document.updated({"CFM_BIND_PORT": "8001"}).lines[0].startswith(
        "FUTURE_SETTING=$(touch"
    )
    assert not marker.exists()


def test_symlinked_environment_file_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("CFM_BIND_PORT=8000\n", encoding="utf-8")
    link = tmp_path / "cloudflared-manager.env"
    link.symlink_to(target)

    with pytest.raises(EnvironmentFileError, match="regular file"):
        read_environment(link)
    with pytest.raises(EnvironmentFileError, match="symlinked"):
        atomic_write_environment(link, b"CFM_BIND_PORT=9000\n", owner=None)


def test_duplicate_managed_key_is_rejected() -> None:
    document = EnvironmentDocument.parse(
        "CFM_BIND_PORT=8000\nCFM_BIND_PORT=9000\n"
    )

    with pytest.raises(EnvironmentFileError, match="duplicate"):
        document.managed_values()


def test_sensitive_unknown_setting_is_preserved_opaquely() -> None:
    fake_value = "TEST_API_VALUE_MUST_NOT_LEAK"
    document = EnvironmentDocument.parse(
        f"CFM_API_TOKEN={fake_value}\nCFM_BIND_PORT=8000\n"
    )

    assert document.managed_values() == {"CFM_BIND_PORT": "8000"}
    updated = document.updated({"CFM_BIND_PORT": "8081"})

    assert updated.lines[0] == f"CFM_API_TOKEN={fake_value}"
    assert updated.lines[1] == "CFM_BIND_PORT=8081"
