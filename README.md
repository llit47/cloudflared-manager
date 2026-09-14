# Cloudflared Manager

Cloudflared Manager is a small, LAN-only web application for managing services
on an existing, locally operated Cloudflare Tunnel. The project is intended to
coordinate ingress configuration, explicitly owned DNS routes, validation, and
service activation without exposing the management UI through the tunnel.

## Project status

The project currently provides a runnable FastAPI service with typed settings,
a server-rendered dashboard, a minimal health endpoint, isolated tests, and CI.
When an explicit cloudflared configuration path is provided, the dashboard
reads it and displays detected hostname ingress routes in read-only mode.
Optional local runtime discovery can also report sanitized cloudflared binary
and systemd service facts when it is explicitly enabled.

Detected routes are existing configuration, not routes owned or managed by
Cloudflared Manager. This version does **not** modify cloudflared configuration,
contact the Cloudflare API, change DNS, control cloudflared or systemd, configure
sudo, persist ownership, or implement Add, Edit, Enable, Disable, or Delete.

## Architecture

The application uses Python 3.12+, FastAPI, Jinja2 templates, and plain CSS.
Tests use HTTPX's in-process ASGI transport. The project uses a `src/` package
layout:

- `cloudflared_manager.config` owns typed application settings and safe local
  defaults.
- `cloudflared_manager.main` creates the FastAPI application and mounts static
  assets.
- `cloudflared_manager.cloudflared` contains read-only domain models, safe
  parser errors, YAML parsing that accepts only an explicit path, sanitized
  runtime discovery, and an allowlisted local command runner.
- `cloudflared_manager.web` keeps thin HTTP routes separate from dashboard
  presentation models and server-rendered UI code.
- `templates/` and `static/` define the accessible, responsive dashboard shell.
- `tests/fixtures/cloudflared/config.yml` is entirely fake documentation and
  test input. The running application does not use it by default.

Future configuration mutation, DNS, service-control, persistence, and
transaction code will remain outside the HTTP and presentation layers. No
speculative integration interfaces or empty database are included.

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
| `CFM_CLOUDFLARED_CONFIG_PATH` | unset | Explicit cloudflared YAML file to read |
| `CFM_RUNTIME_DISCOVERY_ENABLED` | `false` | Enable read-only local runtime inspection (`true` or `false`) |

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

### Read-only local runtime discovery

Runtime discovery is disabled by default so development and test runs do not
unexpectedly inspect the host. Enable it explicitly with:

```bash
CFM_RUNTIME_DISCOVERY_ENABLED=true python -m cloudflared_manager.main
```

When enabled on Linux, discovery may execute only these fixed, read-only local
commands with finite timeouts and without a shell:

```text
<resolved-cloudflared-path> --version
<resolved-systemctl-path> show cloudflared.service --no-pager --property=LoadState,ActiveState,SubState,MainPID,ExecStart
<resolved-systemctl-path> is-enabled cloudflared.service
```

The resulting dashboard status distinguishes an installed binary from a loaded
systemd unit and an active/running service. It does not test Cloudflare network
connectivity. Missing commands, a missing unit, inactive or failed services,
timeouts, and unexpected output are handled as best-effort observations rather
than application startup failures.

Systemd `ExecStart` data is treated as potentially secret-bearing because a
token-managed service can include a token argument. Discovery extracts only
the executable, management mode, and an explicit config argument, then discards
the raw value. Raw command output, tokens, and full local paths are not passed
to dashboard presentation models.

Discovery is strictly observational: it never starts, stops, restarts, reloads,
enables, disables, or edits cloudflared or systemd. A config path found in
service arguments is not automatically adopted or read. The separate
`CFM_CLOUDFLARED_CONFIG_PATH` setting remains required for YAML loading, and it
still has no default. A future production installer can reuse these discovery
facts to configure the manager through an explicit reviewed workflow.

### Read-only cloudflared configuration

To preview detection safely with the fake test fixture:

```bash
CFM_CLOUDFLARED_CONFIG_PATH=tests/fixtures/cloudflared/config.yml \
  python -m cloudflared_manager.main
```

The reader currently projects only the tunnel identifier, ordered ingress
rules, hostname and path matchers, service targets, and the terminal catch-all.
The catch-all remains in the parsed domain model to preserve ordering but is
not displayed or counted as a hostname route. The reader does not load the
credentials file referenced by the YAML.

YAML support uses the mature PyYAML dependency and its `safe_load` API; no
custom YAML parser or unsafe object construction is used.

Configuration loading is strictly read-only. It does not write YAML, create
backups, run cloudflared, contact Cloudflare, change DNS, or control system
services. A missing or invalid explicit file produces a safe dashboard error
while the application and `/healthz` remain available.

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

Production configuration adoption and mutation, DNS management, system service
control, transactional rollback, and managed-record ownership tracking remain
deferred to later reviewed changes.

## Run tests

With the development dependencies installed:

```bash
python -m pytest
```

The tests use only in-process HTTP requests, deterministic fake command
runners, mocked subprocess calls, and temporary paths. They require no network
services, never invoke the machine's cloudflared or systemd, and never access
`/etc/cloudflared`.
