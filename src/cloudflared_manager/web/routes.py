"""Routes for the dashboard shell and monitoring health check."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from cloudflared_manager.web.presentation import build_dashboard_view

router = APIRouter()
templates = Jinja2Templates(
    directory=Path(__file__).parent.parent / "templates",
)


@router.get("/", response_class=HTMLResponse, name="dashboard")
async def dashboard(request: Request) -> HTMLResponse:
    """Render the read-only dashboard."""

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "settings": request.app.state.settings,
            "dashboard": build_dashboard_view(request.app.state.settings),
        },
    )


@router.get("/healthz", name="health")
async def health() -> dict[str, str]:
    """Return a minimal response that reveals no runtime configuration."""

    return {"status": "ok", "app": "cloudflared-manager"}
