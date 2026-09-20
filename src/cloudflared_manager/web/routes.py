"""Routes for the dashboard shell and monitoring health check."""

import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from cloudflared_manager.web.presentation import build_dashboard_view
from cloudflared_manager.runtime_identity import PROCESS_RELEASE_ID

router = APIRouter()
templates = Jinja2Templates(
    directory=Path(__file__).parent.parent / "templates",
)


@router.get("/", response_class=HTMLResponse, name="dashboard")
def dashboard(request: Request) -> HTMLResponse:
    """Render the read-only dashboard."""

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "settings": request.app.state.settings,
            "dashboard": build_dashboard_view(
                request.app.state.settings,
                discover_runtime=request.app.state.runtime_discovery,
            ),
        },
    )


@router.get("/healthz", name="health")
async def health() -> dict[str, str]:
    """Return a minimal response that reveals no runtime configuration."""

    return {"status": "ok", "app": "cloudflared-manager"}


@router.get("/deployment-readiness", name="deployment-readiness")
async def deployment_readiness(request: Request) -> dict[str, str | int | None]:
    """Return only the identity needed by local deployment transactions."""

    return {
        "status": "ready",
        "app": "cloudflared-manager",
        "pid": os.getpid(),
        "config_id": request.app.state.settings.config_id,
        "release_id": PROCESS_RELEASE_ID,
    }
