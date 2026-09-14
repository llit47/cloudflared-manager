import asyncio

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from cloudflared_manager.config import Settings
from cloudflared_manager.main import create_app


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


def test_dashboard_renders_foundation_ui() -> None:
    response = get_from_app(create_app(Settings(mode="test")), "/")

    assert response.status_code == 200
    assert "Cloudflared Manager" in response.text
    assert "Tunnel" in response.text
    assert "cloudflared" in response.text
    assert "Configuration" in response.text
    assert "Managed Services" in response.text
    assert "Add service" in response.text
    assert "No managed services yet" in response.text
    assert "disabled" in response.text


def test_healthz_returns_only_safe_monitoring_fields() -> None:
    response = get_from_app(create_app(Settings(mode="test")), "/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "app": "cloudflared-manager",
    }
