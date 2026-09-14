from pathlib import Path

from cloudflared_manager.cloudflared import (
    CloudflaredConfig,
    ConfigFileNotFoundError,
    IngressRule,
)
from cloudflared_manager.config import Settings
from cloudflared_manager.web.presentation import build_dashboard_view


def test_no_path_does_not_call_config_loader() -> None:
    def unexpected_loader(path: Path) -> CloudflaredConfig:
        raise AssertionError(f"loader called with {path}")

    view = build_dashboard_view(Settings(mode="test"), unexpected_loader)

    assert view.configuration.label == "Not configured"
    assert view.routes == ()


def test_presentation_excludes_catch_all_and_redacts_url_secrets() -> None:
    config = CloudflaredConfig(
        tunnel="fake-tunnel",
        ingress_rules=(
            IngressRule(
                hostname="app.example.com",
                path="^/private/.*",
                service="https://user:password@localhost:8443/base?token=secret#value",
            ),
            IngressRule(service="http_status:404", is_catch_all=True),
        ),
    )

    view = build_dashboard_view(
        Settings(mode="test", cloudflared_config_path=Path("fixture.yml")),
        lambda path: config,
    )

    assert view.route_count == 1
    assert view.routes[0].service == "https://localhost:8443/base"
    assert "password" not in view.routes[0].service
    assert "secret" not in view.routes[0].service
    assert "http_status:404" not in {route.service for route in view.routes}


def test_expected_loader_error_becomes_safe_dashboard_state() -> None:
    def missing_loader(path: Path) -> CloudflaredConfig:
        raise ConfigFileNotFoundError("safe internal error")

    view = build_dashboard_view(
        Settings(
            mode="test",
            cloudflared_config_path=Path("/private/secret-config.yml"),
        ),
        missing_loader,
    )

    assert view.configuration.label == "Load error"
    assert view.routes == ()
    assert "/private" not in view.configuration.description
    assert "secret-config" not in view.configuration.description
