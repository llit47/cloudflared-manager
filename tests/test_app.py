import asyncio
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
import pytest

from cloudflared_manager.cloudflared import CommandTimedOutError
from cloudflared_manager.cloudflared.discovery import discover_cloudflared
from cloudflared_manager.cloudflared.runtime import CommandResult, DiscoveryCommand
from cloudflared_manager.config import Settings
from cloudflared_manager.main import create_app
from cloudflared_manager.runtime_identity import runtime_config_id
from cloudflared_manager.web.presentation import build_dashboard_view

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "cloudflared" / "config.yml"
FAKE_SECRET = "TEST_SECRET_MUST_NOT_LEAK"
FAKE_TOKEN_FILE_PATH = "/nonexistent/test-token-file"


def get_from_app(application: FastAPI, path: str) -> Response:
    async def get() -> Response:
        transport = ASGITransport(app=application)
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.get(path)

    return asyncio.run(get())


def test_application_can_be_created(tmp_path) -> None:
    settings = Settings(
        mode="test",
        cloudflared_config_path=tmp_path / "config.yml",
    )

    application = create_app(settings)

    assert isinstance(application, FastAPI)
    assert application.state.settings is settings


def test_dashboard_without_configured_path_renders_safe_empty_state() -> None:
    response = get_from_app(create_app(Settings(mode="test")), "/")

    assert response.status_code == 200
    assert "Cloudflared Manager" in response.text
    assert "Tunnel" in response.text
    assert "cloudflared" in response.text
    assert "Configuration" in response.text
    assert "Not configured" in response.text
    assert "Detected ingress routes" in response.text
    assert "Add service" in response.text
    assert "No configuration selected" in response.text
    assert "disabled" in response.text


def test_dashboard_renders_detected_routes_from_explicit_fixture() -> None:
    settings = Settings(mode="test", cloudflared_config_path=FIXTURE_PATH)

    response = get_from_app(create_app(settings), "/")

    assert response.status_code == 200
    assert "Loaded" in response.text
    assert "2 hostname routes detected" in response.text
    assert "dashboard.example.com" in response.text
    assert "photos.example.com" in response.text
    assert "^/admin/.*" in response.text
    assert "All paths" in response.text
    assert "http://localhost:8080" in response.text
    assert "http://localhost:3000" in response.text
    assert "Configured · Read only" in response.text
    assert "http_status:404" not in response.text
    assert "00000000-0000-4000-8000-000000000000" not in response.text
    assert "/nonexistent/cloudflared/example-tunnel.json" not in response.text


def test_loaded_config_with_only_catch_all_renders_loaded_empty_state(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        """ingress:
  - service: http_status:404
""",
        encoding="utf-8",
    )
    settings = Settings(mode="test", cloudflared_config_path=config_path)

    response = get_from_app(create_app(settings), "/")

    assert response.status_code == 200
    assert "Loaded" in response.text
    assert "0 hostname routes detected" in response.text
    assert "No hostname routes detected" in response.text
    assert 'aria-label="0 detected ingress routes"' in response.text
    assert "http_status:404" not in response.text


def test_dashboard_survives_invalid_explicit_config_without_path_disclosure(
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "private-token-config.yml"
    settings = Settings(mode="test", cloudflared_config_path=missing_path)

    response = get_from_app(create_app(settings), "/")

    assert response.status_code == 200
    assert "Load error" in response.text
    assert "Routes unavailable" in response.text
    assert str(missing_path) not in response.text
    assert "private-token-config.yml" not in response.text
    assert "Traceback" not in response.text


def test_dashboard_survives_invalid_yaml_without_content_disclosure(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "api_token: do-not-display\ningress: [",
        encoding="utf-8",
    )
    settings = Settings(mode="test", cloudflared_config_path=config_path)

    response = get_from_app(create_app(settings), "/")

    assert response.status_code == 200
    assert "Load error" in response.text
    assert "do-not-display" not in response.text
    assert "api_token" not in response.text
    assert "Traceback" not in response.text


def test_healthz_returns_only_safe_monitoring_fields() -> None:
    settings = Settings(
        mode="test",
        cloudflared_config_path=Path("/private/config/should-not-appear.yml"),
    )

    response = get_from_app(create_app(settings), "/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "app": "cloudflared-manager",
    }


def test_deployment_readiness_identity_includes_adopted_config_path() -> None:
    config_path = Path("/etc/cloudflared/config.yml")
    settings = Settings(
        mode="test",
        cloudflared_config_path=config_path,
        runtime_discovery_enabled=True,
    )

    response = get_from_app(create_app(settings), "/deployment-readiness")

    assert response.status_code == 200
    assert response.json()["config_id"] == runtime_config_id(
        "127.0.0.1", 8000, True, config_path
    )


@pytest.mark.parametrize(
    ("token_argument", "sensitive_value"),
    [
        (f"--token {FAKE_SECRET}", FAKE_SECRET),
        (f"--token={FAKE_SECRET}", FAKE_SECRET),
        (f"--token-file {FAKE_TOKEN_FILE_PATH}", FAKE_TOKEN_FILE_PATH),
        (f"--token-file={FAKE_TOKEN_FILE_PATH}", FAKE_TOKEN_FILE_PATH),
    ],
)
def test_dashboard_renders_sanitized_token_managed_runtime(
    token_argument: str,
    sensitive_value: str,
) -> None:
    class TokenServiceRunner:
        def run(
            self,
            command: DiscoveryCommand,
            *,
            cloudflared_executable: Path | None = None,
        ) -> CommandResult:
            if command is DiscoveryCommand.SYSTEMD_SHOW:
                return CommandResult(
                    returncode=0,
                    stdout=(
                        "LoadState=loaded\n"
                        "ActiveState=active\n"
                        "SubState=running\n"
                        "MainPID=4242\n"
                        "ExecStart={ path=/opt/example/bin/cloudflared ; "
                        "argv[]=/opt/example/bin/cloudflared tunnel run "
                        f"{token_argument} ; ignore_errors=no ; }}\n"
                    ),
                    stderr="",
                )
            if command is DiscoveryCommand.SYSTEMD_IS_ENABLED:
                return CommandResult(returncode=0, stdout="enabled\n", stderr="")
            raise AssertionError(f"unexpected command: {command}")

    runtime = discover_cloudflared(
        True,
        TokenServiceRunner(),
        executable_finder=lambda name: None,
        executable_checker=lambda path: False,
    )
    settings = Settings(mode="test", runtime_discovery_enabled=True)
    dashboard = build_dashboard_view(
        settings,
        discover_runtime=lambda enabled: runtime,
    )

    response = get_from_app(
        create_app(settings, runtime_discovery=lambda enabled: runtime),
        "/",
    )

    assert response.status_code == 200
    assert "Running" in response.text
    assert "active/running" in response.text
    assert "Token-managed mode detected" in response.text
    assert "Not configured" in response.text
    assert "No configuration selected" in response.text
    assert sensitive_value not in repr(runtime)
    assert sensitive_value not in repr(dashboard)
    assert sensitive_value not in response.text
    assert "ExecStart" not in response.text
    assert "/opt/example" not in response.text


def test_dashboard_survives_runtime_discovery_error_without_disclosure() -> None:
    def failed_discovery(enabled: bool):
        raise CommandTimedOutError(FAKE_SECRET)

    settings = Settings(mode="test", runtime_discovery_enabled=True)

    response = get_from_app(
        create_app(settings, runtime_discovery=failed_discovery),
        "/",
    )

    assert response.status_code == 200
    assert "Unavailable" in response.text
    assert FAKE_SECRET not in response.text
    assert "Traceback" not in response.text


def test_healthz_does_not_run_enabled_runtime_discovery() -> None:
    def unexpected_discovery(enabled: bool):
        raise AssertionError("health endpoint triggered runtime discovery")

    settings = Settings(mode="test", runtime_discovery_enabled=True)

    response = get_from_app(
        create_app(settings, runtime_discovery=unexpected_discovery),
        "/healthz",
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "app": "cloudflared-manager"}
