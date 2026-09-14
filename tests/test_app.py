import asyncio
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from cloudflared_manager.config import Settings
from cloudflared_manager.main import create_app

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "cloudflared" / "config.yml"


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
