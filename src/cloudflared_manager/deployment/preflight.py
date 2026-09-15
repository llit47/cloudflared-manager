"""Candidate release import and application-construction preflight."""

from cloudflared_manager.config import Settings
from cloudflared_manager.main import create_app


def main() -> int:
    application = create_app(
        Settings(
            mode="production",
            bind_host="127.0.0.1",
            runtime_discovery_enabled=False,
        )
    )
    if application.title != "Cloudflared Manager":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
