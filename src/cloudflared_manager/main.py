"""FastAPI application construction and local server entry point."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from cloudflared_manager.cloudflared.discovery import (
    RuntimeDiscoveryProvider,
    discover_cloudflared,
)
from cloudflared_manager.config import Settings
from cloudflared_manager.web.routes import router

PACKAGE_DIR = Path(__file__).parent


def create_app(
    settings: Settings | None = None,
    runtime_discovery: RuntimeDiscoveryProvider = discover_cloudflared,
) -> FastAPI:
    """Create an application instance with explicit, replaceable settings."""

    app_settings = settings or Settings.from_env()
    application = FastAPI(
        title=app_settings.app_name,
        description="LAN-only manager for a locally operated Cloudflare Tunnel.",
    )
    application.state.settings = app_settings
    application.state.runtime_discovery = runtime_discovery
    application.mount(
        "/static",
        StaticFiles(directory=PACKAGE_DIR / "static"),
        name="static",
    )
    application.include_router(router)
    return application


app = create_app()


def run() -> None:
    """Run the development server using the configured bind address."""

    import uvicorn

    settings = Settings.from_env()
    uvicorn.run(
        "cloudflared_manager.main:app",
        host=settings.bind_host,
        port=settings.bind_port,
        reload=settings.mode == "development",
    )


if __name__ == "__main__":
    run()
