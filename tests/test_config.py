from pathlib import Path

import pytest

from cloudflared_manager.config import Settings
from cloudflared_manager.runtime_identity import runtime_config_id


def test_settings_defaults_are_safe_for_local_development() -> None:
    settings = Settings.from_env({})

    assert settings.app_name == "Cloudflared Manager"
    assert settings.mode == "development"
    assert settings.bind_host == "127.0.0.1"
    assert settings.bind_port == 8000
    assert settings.cloudflared_config_path is None
    assert settings.runtime_discovery_enabled is False
    assert settings.cloudflared_config_path != Path("/etc/cloudflared/config.yml")


def test_settings_accept_explicit_environment_values(tmp_path) -> None:
    config_path = tmp_path / "cloudflared.yml"

    settings = Settings.from_env(
        {
            "CFM_APP_NAME": "Tunnel Console",
            "CFM_MODE": "test",
            "CFM_BIND_HOST": "localhost",
            "CFM_BIND_PORT": "8123",
            "CFM_CLOUDFLARED_CONFIG_PATH": str(config_path),
            "CFM_RUNTIME_DISCOVERY_ENABLED": "true",
        }
    )

    assert settings == Settings(
        app_name="Tunnel Console",
        mode="test",
        bind_host="localhost",
        bind_port=8123,
        cloudflared_config_path=config_path,
        runtime_discovery_enabled=True,
    )
    assert settings.config_id == runtime_config_id(
        "localhost", 8123, True, config_path
    )


@pytest.mark.parametrize("port", ["zero", "0", "65536"])
def test_settings_reject_invalid_ports(port: str) -> None:
    with pytest.raises(ValueError):
        Settings.from_env({"CFM_BIND_PORT": port})


def test_settings_reject_unknown_mode() -> None:
    with pytest.raises(ValueError, match="mode must be"):
        Settings.from_env({"CFM_MODE": "staging"})


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), (" TRUE ", True), ("false", False), ("FALSE", False)],
)
def test_settings_parse_runtime_discovery_boolean_strictly(
    value: str,
    expected: bool,
) -> None:
    settings = Settings.from_env({"CFM_RUNTIME_DISCOVERY_ENABLED": value})

    assert settings.runtime_discovery_enabled is expected


@pytest.mark.parametrize("value", ["", "1", "yes", "enabled", "sometimes"])
def test_settings_reject_invalid_runtime_discovery_boolean(value: str) -> None:
    with pytest.raises(ValueError, match="must be true or false"):
        Settings.from_env({"CFM_RUNTIME_DISCOVERY_ENABLED": value})
