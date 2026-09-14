# Cloudflared Manager

Cloudflared Manager is a small, LAN-only web application for managing services
on an existing, locally operated Cloudflare Tunnel. The project is intended to
coordinate ingress configuration, explicitly owned DNS routes, validation, and
service activation without exposing the management UI through the tunnel.

## Project status

The project currently provides its application foundation: a runnable FastAPI
service, typed settings, a server-rendered dashboard shell, a minimal health
endpoint, isolated tests, and CI. The dashboard reports honest unconfigured
states and its **Add service** action is disabled.

This version does **not** read or modify cloudflared configuration, contact the
Cloudflare API, change DNS, invoke cloudflared or systemd, configure sudo, or
persist manager state. It does not implement Add, Edit, Enable, Disable, or
Delete operations. Those integrations require separate reviewed changes.

## Architecture

The application uses Python 3.12+, FastAPI, Jinja2 templates, and plain CSS.
Tests use HTTPX's in-process ASGI transport. The project uses a `src/` package
layout:

- `cloudflared_manager.config` owns typed application settings and safe local
  defaults.
- `cloudflared_manager.main` creates the FastAPI application and mounts static
  assets.
- `cloudflared_manager.web` contains HTTP routes and server-rendered UI code.
- `templates/` and `static/` define the accessible, responsive dashboard shell.
- `tests/fixtures/cloudflared/config.yml` is fake documentation and test input
  for later configuration work. The running application does not use it by
  default.

Future configuration, DNS, service-control, persistence, and transaction code
will live outside the HTTP layer. No speculative integration interfaces or
empty database are included in the foundation.

## Prerequisites

- Python 3.12 or newer
- `pip` and Python's `venv` module

Cloudflare credentials, cloudflared, systemd, root access, and network access
are not required to install or test this version.

## Local installation

Create an isolated environment and install the package with its development
dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

This repository uses `pyproject.toml` as its single dependency and packaging
configuration.

## Configuration

Settings can be provided through these environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CFM_APP_NAME` | `Cloudflared Manager` | Application and dashboard name |
| `CFM_MODE` | `development` | `development`, `test`, or `production` |
| `CFM_BIND_HOST` | `127.0.0.1` | Server bind address |
| `CFM_BIND_PORT` | `8000` | Server port |
| `CFM_CLOUDFLARED_CONFIG_PATH` | unset | Optional path reserved for later integration |

The application does not load `.env` automatically. `.env.example` contains
safe sample values that can be exported by a shell if needed:

```bash
cp .env.example .env
set -a
source .env
set +a
```

The cloudflared path intentionally has no default. In particular, the
application never implicitly selects `/etc/cloudflared/config.yml`.

## Run the development server

The package entry point reads `CFM_BIND_HOST` and `CFM_BIND_PORT` and enables
reload mode when `CFM_MODE=development`:

```bash
python -m cloudflared_manager.main
```

The equivalent explicit Uvicorn command for the safe development defaults is:

```bash
uvicorn cloudflared_manager.main:app --reload --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/> for the dashboard. The monitoring endpoint is
available at <http://127.0.0.1:8000/healthz>.

The localhost bind is deliberate. A production LAN bind must be explicitly
configured during deployment; the manager must not be exposed through a public
listener or Cloudflare Tunnel route.

## Run tests

With the development dependencies installed:

```bash
python -m pytest
```

The tests use only in-process HTTP requests and temporary paths. They require
no network services and never access `/etc/cloudflared`.
