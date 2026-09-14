from pathlib import Path

import pytest

from cloudflared_manager.config import Settings


def test_settings_defaults_are_safe_for_local_development() -> None:
    settings = Settings.from_env({})

    assert settings.app_name == "Cloudflared Manager"
    assert settings.mode == "development"
    assert settings.bind_host == "127.0.0.1"
    assert settings.bind_port == 8000
    assert settings.cloudflared_config_path is None
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
        }
    )

    assert settings == Settings(
        app_name="Tunnel Console",
        mode="test",
        bind_host="localhost",
        bind_port=8123,
        cloudflared_config_path=config_path,
    )


@pytest.mark.parametrize("port", ["zero", "0", "65536"])
def test_settings_reject_invalid_ports(port: str) -> None:
    with pytest.raises(ValueError):
        Settings.from_env({"CFM_BIND_PORT": port})


def test_settings_reject_unknown_mode() -> None:
    with pytest.raises(ValueError, match="mode must be"):
        Settings.from_env({"CFM_MODE": "staging"})
