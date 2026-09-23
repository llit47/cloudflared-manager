# Cloudflared Manager

Cloudflared Manager is a small, LAN-only web application for managing services
on an existing, locally operated Cloudflare Tunnel. The project is intended to
coordinate ingress configuration, explicitly owned DNS routes, validation, and
service activation without exposing the management UI through the tunnel.

The management UI is intended for a trusted LAN or a private VPN/overlay
network such as WireGuard or Tailscale; a private VPN may be treated
operationally as an extension of the trusted LAN. Direct public Internet
exposure of the UI is unsupported. An external authentication layer or reverse
proxy does not change the trusted-host/root threat model or make Cloudflared
Manager an Internet-facing zero-trust security boundary.

## Project status

The project currently provides a runnable FastAPI service with typed settings,
a server-rendered dashboard, a minimal health endpoint, isolated tests, and CI.
When an explicit cloudflared configuration path is provided, the dashboard
reads it and displays detected hostname ingress routes in read-only mode.
Optional local runtime discovery can also report sanitized cloudflared binary
and systemd service facts when it is explicitly enabled. The dashboard reports
whether a local config candidate is detected, explicitly adopted, matched to
the running service, unverified, or different from the service configuration.
A production deployment foundation installs immutable release-specific
environments and runs the application as a dedicated unprivileged system user.
An internal candidate-generation foundation can securely snapshot an explicitly
selected config, apply a narrow in-memory ingress insertion, stage a separate
candidate beside the source, and validate that candidate. Nothing in the web
application or deployment workflow invokes this foundation yet.

Detected routes are existing configuration, not routes owned or managed by
Cloudflared Manager. This version does **not** modify cloudflared configuration,
contact the Cloudflare API, change DNS, control `cloudflared.service`, configure
sudo, persist ownership, or implement Add, Edit, Enable, Disable, or Delete.
Deployment scripts control only `cloudflared-manager.service`.

## Architecture

The application uses Python 3.12+, FastAPI, Jinja2 templates, and plain CSS.
Tests use HTTPX's in-process ASGI transport. The project uses a `src/` package
layout:

- `cloudflared_manager.config` owns typed application settings and safe local
  defaults.
- `cloudflared_manager.main` creates the FastAPI application and mounts static
  assets.
- `cloudflared_manager.cloudflared` contains read-only domain models, safe
  parser errors, strict read-domain YAML parsing that accepts only an explicit
  path, sanitized runtime discovery, and an allowlisted local command runner.
- `cloudflared_manager.cloudflared.editing` contains the separate internal
  round-trip document, secure source snapshot, candidate-file lifecycle, and
  candidate validation boundaries. It exposes no activation operation.
- `cloudflared_manager.web` keeps thin HTTP routes separate from dashboard
  presentation models and server-rendered UI code.
- `cloudflared_manager.deployment` separates exact-source bootstrap, input and
  LAN validation, environment-file handling, release filesystem operations,
  fixed systemd commands, health checks, and install/update/config transactions.
- `install.sh` and `deploy/` provide the small root-facing bootstrap, stable
  command wrappers, and hardened manager-only systemd unit.
- `templates/` and `static/` define the accessible, responsive dashboard shell.
- `tests/fixtures/cloudflared/config.yml` is entirely fake documentation and
  test input. The running application does not use it by default.

Future cloudflared configuration mutation, DNS, cloudflared service-control,
persistence, and transaction code will remain outside the HTTP and presentation
layers. No speculative integration interfaces or empty database are included.

## Prerequisites

- Python 3.12 or newer
- `pip` and Python's `venv` module

Cloudflare credentials, cloudflared, systemd, root access, and network access
are not required for development installation or tests.

## Production deployment

Production deployment targets Linux hosts using systemd and Python 3.12 or
newer. It creates one Cloudflared Manager installation. This release exposes
only **READ-ONLY** capability; there is no hidden read/write switch and no
Cloudflare API token is accepted or required.

Install the current `main` revision non-interactively:

```bash
curl -fsSL https://raw.githubusercontent.com/llit47/cloudflared-manager/main/install.sh | sudo bash
```

The bootstrap resolves `main` through the public GitHub API, validates the
result as an exact 40-character Git SHA, downloads the bootstrap and source
archive for that immutable revision over HTTPS, rejects unsafe archive paths
and entry types, then prepares the release. Git and a GitHub token are not
required on the production host. The selected Python must already be version
3.12 or newer and include `venv`; otherwise installation fails with an
actionable message rather than installing an alternate Python distribution.

Installation never prompts on stdin, because stdin carries the installer
itself. It chooses the interface used by the lowest-metric IPv4 default route,
then accepts the address only when exactly one global RFC1918 address is
present on that interface. No address or multiple equally plausible addresses
causes a safe failure. It never chooses `0.0.0.0`, loopback, link-local,
multicast, or a public address.

An administrator can supply a strictly validated private address and
unprivileged port when automatic selection is unsuitable:

```bash
curl -fsSL https://raw.githubusercontent.com/llit47/cloudflared-manager/main/install.sh \
  | sudo env CFM_INSTALL_BIND_HOST=192.168.1.20 CFM_INSTALL_BIND_PORT=8000 bash
```

An explicit address must also be present on a local global IPv4 interface at
the time it is configured. It may belong to a secondary LAN interface and does
not need to be on the default-route interface.

The supported port range is 1024 through 65535. The installer does not add
firewall rules or expose the manager through a Cloudflare Tunnel.

### Production layout and identity

| Path | Intended ownership/mode | Purpose |
| --- | --- | --- |
| `/opt/cloudflared-manager/` | `root:root`, `0755` | Root-owned deployment root |
| `/opt/cloudflared-manager/releases/<sha>/` | `root:root`, no group/other writes | Exact source and release-local `.venv` |
| `/opt/cloudflared-manager/current` | root-owned atomic symlink | Active immutable release |
| `/opt/cloudflared-manager/update.sh` | `root:root`, `0755` | Stable updater |
| `/opt/cloudflared-manager/config.sh` | `root:root`, `0755` | Stable configurator |
| `/etc/cloudflared-manager/` | `root:root`, `0750` | Persistent manager configuration |
| `/etc/cloudflared-manager/cloudflared-manager.env` | `root:root`, `0600` | systemd EnvironmentFile |
| `/etc/systemd/system/cloudflared-manager.service` | `root:root`, `0644` | Manager service unit |
| `/usr/local/sbin/cfm-update` | root-owned symlink | Stable update command |
| `/usr/local/sbin/cfm-config` | root-owned symlink | Stable configuration command |

The `cloudflared-manager` system user and group are system identities with
home `/nonexistent`, a non-login shell, and no supplementary or privileged
group membership. The service runs as this user. Application releases,
deployment scripts, and persistent configuration remain root-owned and are
not writable by the service process. Existing cloudflared users, groups,
permissions, and files are never altered.

For the supported Debian/Ubuntu deployment target, an existing identity must
also use the conventional system-account UID/GID range below 1000. That
numeric boundary is a platform assumption used to reject collisions with
ordinary login accounts; the actual privilege boundary is the non-root,
non-login identity with no supplementary groups and no writable deployment
files.

The EnvironmentFile initially contains only the application name, production
mode, selected concrete bind address and port, and
`CFM_RUNTIME_DISCOVERY_ENABLED=true`. It does not contain
`CFM_CLOUDFLARED_CONFIG_PATH` or a Cloudflare token. A root administrator can
later add the optional path only through the explicit detected-config adoption
command described below. Fresh installs, installer reruns, updates, and service
starts never adopt a path automatically. Environment data is parsed as data and
is never sourced or evaluated as shell code. Unknown keys and comments,
including future secret-bearing assignments, are preserved opaquely by
supported updates. This release does not interpret, print, request, or use
those unknown values.

### Service and health model

`cloudflared-manager.service` starts the console entry point from
`/opt/cloudflared-manager/current/.venv/` as the dedicated service user. The
unit enables automatic restart on failure, removes all capabilities, and uses
systemd protections including `NoNewPrivileges`, `PrivateTmp`,
`PrivateDevices`, `ProtectHome`, `ProtectSystem=strict`, kernel/control-group
protections, and a restricted address-family set.

`GET /healthz` is the minimal public monitoring endpoint. Its response contract
remains exactly:

```json
{"status":"ok","app":"cloudflared-manager"}
```

Deployment transactions use the separate internal `GET /deployment-readiness`
endpoint. Installation, update, and configuration success additionally require
the readiness responder PID to match the systemd manager `MainPID`, require that
`MainPID` to remain stable across verification, require the returned `config_id`
to match the expected persisted runtime configuration, and confirm that the
managed service remains active. Once adopted, the config path participates in
that identity, proving that a restarted process loaded the requested setting.
When no path is adopted, the identity retains the exact legacy three-field hash
used by releases before adoption support so in-place updates remain compatible.
The readiness response contains no paths, environment contents, command lines,
or secrets.

Runtime discovery is enabled in production. It performs only the read-only
observations documented below. A systemd-discovered cloudflared configuration
path remains an observation until a root administrator explicitly adopts it.
Installer output does not adopt it and reports only sanitized binary and
service state, never raw `ExecStart`, tokens, token-file paths, credentials, or
unrelated command-line contents.

### Administration

Run the interactive configuration menu with:

```bash
sudo cfm-config
```

Automation-friendly commands are also available:

```bash
sudo cfm-config status
sudo cfm-config set-bind 192.168.1.20
sudo cfm-config set-port 8000
sudo cfm-config discovery enable
sudo cfm-config discovery disable
sudo cfm-config cloudflared-config status
sudo cfm-config cloudflared-config adopt-detected
sudo cfm-config cloudflared-config clear
```

Status shows the bind address, port, runtime discovery setting, sanitized
manager service state, **READ-ONLY** capability, and that Cloudflare API setup
is unsupported. `cloudflared-config status` separately distinguishes an
adopted path, a detected local-config candidate, disabled/unavailable
discovery, token-managed mode, unknown mode, and a missing explicit candidate.
It never prints raw `ExecStart` or token-bearing arguments.

`cloudflared-config adopt-detected` accepts no path argument. It uses only the
explicit `--config` path extracted from `cloudflared.service` by enabled runtime
discovery, and only when the service is detected in local-config mode. Before
changing manager state it requires a canonical absolute `.yml`/`.yaml` path,
rejects locations hidden by the manager unit's current `ProtectHome=true` and
`PrivateTmp=true` sandbox, rejects symlinks and non-regular or oversized files,
checks conventional Unix read/traverse permissions for the dedicated service
identity, and parses the file with the existing safe read-only parser. Missing,
unreadable, structurally invalid, remote-token, unknown-mode, and pathless
candidates fail closed.

The service-readability check deliberately does not change cloudflared file
ownership or mode. It accounts for the fixed sandbox settings above and normal
owner/group/other permission bits. Restrictive ACLs, future unit sandbox
changes, or other access-control mechanisms may still prevent access and must
be managed by the administrator. A later loss of access is rendered as a safe
dashboard load error rather than weakening file protections.

`cloudflared-config clear` removes only the manager's optional adopted-path
setting. It does not remove, edit, or otherwise unadopt anything from
cloudflared itself. Re-adopting the same exact path and clearing an already
absent path are healthy no-ops without a restart.

A changed setting is validated before any write. The configurator atomically
replaces the manager EnvironmentFile, restarts only
`cloudflared-manager.service`, and verifies deployment readiness including the
expected config identity. Failed health restores the exact previous file,
restarts the manager, and verifies the restored configuration. The requested
operation returns non-zero even when rollback succeeds; a failed rollback is
reported distinctly. An unchanged value is a no-op and causes no restart.

Update to the latest exact `main` revision with:

```bash
sudo cfm-update
```

The updater holds an advisory lock and resolves and validates the latest SHA.
When that SHA is already active, it still verifies health and reconciles the
manager unit, enablement, stable administration scripts, and command links
from the active release; a complete deployment remains a no-op without a
restart. Otherwise it downloads and validates the exact source archive,
creates the candidate `.venv` directly in its final `releases/<sha>/`
directory, installs dependencies, and runs an application preflight before
switching `current` atomically. Every release has its own environment, and old
releases are retained.

If a candidate includes a changed manager unit, the old unit is retained for
rollback. The updater installs the candidate unit, reloads systemd, switches
the release, restarts only the manager, and checks actual health. Failure
restores the prior release and unit, reloads systemd, restarts the prior
manager, and verifies its health. Stable `update.sh` and `config.sh` files are
replaced atomically only after candidate health succeeds, so a failed update
keeps the previous administration tools. Persistent configuration is never
replaced during update, including an explicitly adopted cloudflared config
path.

The deployment root contains a fixed ownership marker. Installer reruns use a
valid marked `current` release to repair an interrupted first installation
without replacing operator EnvironmentFile values. They can also resume the
narrow pre-`current` state where the requested release is ready and the unit on
disk exactly matches that release. Existing unit, stable script, and
command-link paths are replaced only when they are missing or can be matched to
tracked assets from a ready retained manager release; unknown collisions are
rejected. Install and update transactions reload systemd before switching
`current`, including when an interrupted attempt already wrote the candidate
unit and the bytes therefore appear unchanged on retry.

No persistent transaction journal is present in this release. If power is lost
after `current` switches but before candidate health is verified, the next
installer or same-SHA updater validates and attempts to start the active
release. If it is unhealthy, the operation fails without selecting an
arbitrary older release. Automatic power-loss rollback to the precise prior
release would require durable transaction metadata and remains deferred.

Release-specific virtual environments preserve the dependency set of prior
releases for rollback. Package installation uses isolated Python and pip modes
with the public HTTPS PyPI index, but dependencies are not yet hash-locked;
fresh builds are therefore not guaranteed to be bit-for-bit reproducible.

Useful operational commands include:

```bash
sudo cfm-config status
systemctl status cloudflared-manager.service
journalctl -u cloudflared-manager.service
```

The deployment scripts never start, stop, restart, reload, enable, disable, or
edit the existing `cloudflared.service`. They never modify anything under
`/etc/cloudflared`, install cloudflared, call the Cloudflare API, modify DNS, or
read tunnel tokens, token files, credential JSON, or `cert.pem`.

An automated uninstaller is not included in this release.

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

Config adoption is shown separately from config loading and runtime health.
Detection alone is a warning, not adoption: the dashboard offers only the safe
root command for explicit adoption and never displays or loads the detected
path. A successfully loaded adopted config remains available when runtime
discovery is disabled or unavailable, with its live-service relationship marked
unverified. Token-managed service mode is reported as a valid mode without a
local config candidate.

Systemd `ExecStart` data is treated as potentially secret-bearing because a
token-managed service can include a token argument. Discovery extracts only
the executable, management mode, and an explicit config argument, then discards
the raw value. Raw command output, tokens, and full local paths are not passed
to dashboard presentation models.

Discovery is strictly observational: it never starts, stops, restarts, reloads,
enables, disables, or edits cloudflared or systemd. A config path found in
service arguments is not automatically adopted or read. The separate
`CFM_CLOUDFLARED_CONFIG_PATH` setting remains required for YAML loading, has no
default, and can be persisted in production only by the explicit root
`cfm-config cloudflared-config adopt-detected` workflow.

When an adopted config loads and the service exposes a different explicit local
config path, the dashboard reports read-only drift without showing either path.
It does not adopt the new path, clear the old one, edit configuration, or restart
cloudflared.

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

The existing read-domain parser continues to use the mature PyYAML dependency
and its `safe_load` API. Candidate editing is a separate responsibility and
uses `ruamel.yaml` in round-trip mode to retain human-maintained comments,
mapping order, quoting, anchors/aliases, and unrelated structures to the extent
supported by that library. It rejects duplicate keys, unsupported structures,
unsafe tags, malformed YAML, non-UTF-8 input, and oversized input. A real
mutation is not claimed to preserve formatting byte-for-byte, and an explicit
no-op is never serialized or staged. Neither path uses arbitrary-object YAML
deserialization.

Configuration loading is strictly read-only. It does not write YAML, create
backups, run cloudflared, contact Cloudflare, change DNS, or control system
services. A missing or invalid explicit file produces a safe dashboard error
while the application and `/healthz` remain available. After explicit adoption,
the dashboard uses that file to show the tunnel declaration and sanitized
hostname routes; it never reads or dereferences `credentials-file`, token files,
credentials JSON, or `cert.pem`.

### Internal candidate foundation (not active configuration support)

This release remains operationally **READ-ONLY**. The internal editing package
is candidate-generation infrastructure for later privileged work; it is not
wired to HTTP, dashboard requests, `cfm-config`, installation, or deployment.
There is no Add, Edit, Enable, Disable, Delete, DNS, or Cloudflare API workflow.

Candidate preparation follows this bounded sequence:

1. Open a canonical regular source without following symlinks, read at most the
   shared one-MiB limit, and retain immutable bytes, SHA-256, file identity,
   timestamps, ownership/mode, and parent-directory identity.
2. Load those bytes as UTF-8 round-trip YAML and allow only the narrow primitive
   that inserts a supplied ingress mapping immediately before a valid terminal
   catch-all. Unknown existing data is not projected into a smaller model.
3. For a real change only, exclusively create a random `0600` candidate in the
   source directory, write it completely, and `fsync` it. Retained read-only
   file and directory descriptors pin its identity until discard. The adopted
   source is never opened for writing, replaced, renamed, removed, chmodded, or
   chowned.
4. Run the existing PyYAML application parser against the candidate, then run
   the replaceable external validator with the fixed command shape
   `cloudflared tunnel --config <candidate> ingress validate` without a shell
   and with a finite timeout. The actual config argument is
   `/proc/self/fd/<dirfd>/<candidate>`. Only the verified candidate-directory
   descriptor is inherited through explicit `pass_fds`; all unrelated
   descriptors stay close-on-exec. This binds cloudflared lookup to the staged
   directory even if an ancestor pathname is renamed or swapped. A relative
   reference resolved beside the config still traverses the same pinned source
   directory, while process-working-directory resolution is unchanged.
   Candidate bytes and source identity are checked around validation, and any
   failed preparation removes its candidate.
5. Return either an explicit no-op or a validated, disposable candidate. Stop
   there: no code can activate the candidate in this release.

Raw YAML, local config paths, validator output, and credential-bearing values
are not added to browser models or safe exception messages. Normal dashboard
requests do not create or validate candidates, and the installed service stays
unprivileged. `cloudflared.service` is never restarted, reloaded, started, or
stopped by this foundation.

The next activation-focused change must introduce and review a narrow
privileged boundary; revalidate the adopted path at mutation time; reject stale
or concurrent source changes; preserve exact owner, group, and mode; back up
the exact previous bytes; use same-filesystem atomic activation with file and
directory `fsync`; restart or reload cloudflared and verify process/service
readiness; and roll back on activation failure while proving that the previous
working state was restored. A simple backup copy followed by
`os.replace(candidate, config)` is not treated as a sufficient transaction.
None of those activation, backup, rollback, service-control, sudoers, systemd
privilege, permission-broadening, or production write concerns is implemented
here. Cloudflare API and DNS mutation also remain unimplemented.

The design contract for that future work is
[`docs/activation-transaction.md`](docs/activation-transaction.md). It defines
the trust boundary, stale-write and race requirements, transaction state
machine, metadata and durability rules, minimal crash journal, service/readiness
verification, rollback outcomes, phased implementation plan, and adversarial
test matrix. The document is a design only: it does not enable config writes,
privilege, service control, HTTP mutations, or DNS/API behavior.

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

Cloudflared configuration mutation, DNS management, cloudflared service control,
and managed-record ownership tracking remain deferred to later reviewed
changes. The production adoption workflow is manager-configuration-only and
strictly read-only toward cloudflared.

## Run tests

With the development dependencies installed:

```bash
python -m pytest
```

The tests use only in-process HTTP requests, deterministic fake command
runners, mocked subprocess calls, and temporary paths. They require no network
services, never invoke the machine's cloudflared or systemd, and never access
`/etc/cloudflared`.
