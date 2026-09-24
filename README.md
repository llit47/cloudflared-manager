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
Cloudflared Manager. The web application remains read-only. PR15 provides an
explicitly installed, recovery-only sudo bridge for resuming a PR12–PR14
activation journal. This version does **not** initiate new config mutations,
contact the Cloudflare API, change DNS, persist route ownership, or implement
Add, Edit, Enable, Disable, or Delete.

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
unit enables automatic restart on failure and uses protections including `PrivateTmp`,
`PrivateDevices`, `ProtectHome`, `ProtectSystem=strict`, kernel/control-group
protections, and a restricted address-family set. To permit the explicit
`sudo -n` bridge, `NoNewPrivileges=false`; the capability bounding set contains
only `CAP_CHOWN`, `CAP_DAC_OVERRIDE`, `CAP_FOWNER`, `CAP_SETGID`,
`CAP_SETUID`, and `CAP_SYS_PTRACE`. PR14 checks
`/proc/<MainPID>/environ` and `/proc/<MainPID>/exe`; Linux applies a ptrace
read check when `cloudflared.service` runs under another UID, so recovery needs
`CAP_SYS_PTRACE` in the bounding set. The web unit has no writable mount
exceptions for cloudflared or manager state under `ProtectSystem=strict`. The
sudo launcher starts a fixed transient
root service through `/usr/bin/systemd-run`; that service has its own restricted
mount namespace with only `/etc/cloudflared`, `/etc/cloudflared-manager`, and
`/run/cloudflared-manager` writable for recovery. Before installing the bridge, root
checks that these locations are not writable by the web account. Adoption
checks the cloudflared tree; the production web entry point checks again at
startup. The adopted file cannot be owned or writable by the manager account.
The cloudflared directory and its contents cannot grant manager write access.
Manager-private directories must be root-owned `0750` and `0700`, with a
root-owned `0600` environment file. Unsafe or unverifiable permissions fail
closed. The non-root process receives no ambient capabilities.

Installation places `/etc/tmpfiles.d/cloudflared-manager.conf` with the fixed
`d /run/cloudflared-manager 0700 root root -` rule. The system
`systemd-tmpfiles-setup.service` applies it after `/run` is cleared at boot.
Install, update, reconciliation, and explicit bridge installation also run
`/usr/bin/systemd-tmpfiles --create` for this one file immediately, then
verify the root-owned `0700` directory. An unsafe existing directory is
rejected before tmpfiles can change it; the web startup check stays strict.

The first PR14-to-PR15 update still runs PR14's updater. That updater installs
the PR15 candidate unit, switches `current`, and restarts before it installs
the new administration scripts. The candidate unit therefore has a fixed
pre-start bootstrap: systemd starts a short-lived root transient service from
the root-owned current release, which calls the same validated tmpfiles
installer. It installs the rule before the first PR15 web process starts, so
boot can recreate `/run/cloudflared-manager` without a second update. The
pre-start command remains in the web unit's read-only mount namespace; only
the transient service may write `/etc/tmpfiles.d` and `/run` while applying
the fixed rule. It has a 30-second service lifetime limit and only `CAP_CHOWN`.
If bootstrap fails, service start fails and PR14 restores its prior unit and
release. If later health verification fails, the exact root-owned boot rule
may remain; it is compatible with PR14 and grants no service-account write
authority. Subsequent PR15 reconciliation applies the rule idempotently.

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
   there in the candidate preparation API. The separate internal activation
   transaction is described below.

Raw YAML, local config paths, validator output, and credential-bearing values
are not added to browser models or safe exception messages. Normal dashboard
requests do not create or validate candidates, and the installed service stays
unprivileged. `cloudflared.service` is never restarted, reloaded, started, or
stopped by this foundation.

PR12 and PR14 provide the activation transaction. PR15 exposes only its
`recover()` method through a versioned, recovery-only sudo bridge. No web route
or supported CLI calls `run()` to activate a new candidate. The application
does not gain direct config or service-control permissions.

The internal transaction extends PR12's authenticated atomic config exchange,
exact backup, durable journal, rollback, and recovery barrier with PR14's strict
Linux/systemd service observer. It supports only an already healthy
`cloudflared.service`, `Type=notify`, and an explicit local `--config` resolving
to the root-adopted config. The sole service mutation is a fixed restart.
Success requires a new stable process, executable/config identity checks, and
authenticated cleanup with durable journal retirement. A failed activation
restores the exact old config and verifies a rollback restart. Transitional
systemd jobs retain recovery authority without racing config restoration.
Durable service-verification phases proceed only to cleanup on recovery; later
runtime outages never reopen rollback selection.

The implementation currently requires canonical, regular, root-owned
executables with trusted non-writable ancestry, including fixed
`/usr/bin/systemctl`. Symlink executable paths fail closed. Loaded `ExecStart`
must be a single, unescaped command using `tunnel run`, explicit `--config`
(`--config=...` is also accepted), and optionally `--no-autoupdate`. Unknown
flags, quoted/escaped arguments, token environment overrides, remote-managed
execution, and pending unit reloads fail closed. Observations capture at most
64 KiB, use a five-second command deadline, and compare consistent observations
across a fixed one-second stability interval. Restart has a thirty-second
command deadline. These bounds are internal constants, not caller options.
Readiness follows systemd notify startup plus stable process observations;
it does not promise continuous Cloudflare connectivity or invent a metrics
endpoint.

The normative contract is
[`docs/activation-transaction.md`](docs/activation-transaction.md). All automated
activation tests use temporary configs and fake service/process boundaries.
Host systemd/cloudflared integration has not been exercised by these tests.
Cloudflare API/DNS mutations and application mutation authorization remain
separate future work.

### Privileged recovery bridge

The root administrator enables this boundary explicitly after installing the
PR15 release:

```bash
sudo cfm-config install-bridge
```

This checks the active release identity, root-owned release assets and target
directories, validates the policy with `/usr/sbin/visudo -cf`, installs and
applies the fixed runtime tmpfiles rule, then checks the privileged write
boundary before atomically installing
`/opt/cloudflared-manager/privileged-helper` as root `0755` and
`/etc/sudoers.d/cloudflared-manager-bridge` as root `0440`. An ordinary install
or update does not grant sudo. A missing `visudo`, unsafe collision, or stale
release fails before installing the policy. Repeating the command with matching
assets is idempotent.

The policy permits the `cloudflared-manager` account to execute **only** that
helper as root with **no arguments** and no password. The unprivileged client
uses fixed argv `/usr/bin/sudo -n /opt/cloudflared-manager/privileged-helper`.
The helper launcher uses Bash privileged mode to ignore caller-supplied shell
startup settings, invokes `/usr/bin/readlink` by absolute path before clearing
the environment, then launches only the active root-owned release's Python module
with `-I` through fixed `/usr/bin/systemd-run --system --pipe --wait` arguments
and a fixed minimal environment. The request is one UTF-8 JSON
document of at most 4096 bytes on stdin, currently exactly
`{"version":1,"operation":"recover"}`. Unknown versions, operations, fields,
duplicate keys, trailing data, oversized input, and non-root execution fail
closed. Output is one sanitized JSON object with `version`, `ok`, and `code`;
exit status is 0 only for a verified successful outcome. No paths, YAML,
command text, unit names, or subprocess output cross the protocol.

Recovery derives the active release and adopted path again as root, accepts
only an adopted config directly under `/etc/cloudflared`, and invokes the
existing PR14 `FilesystemActivation.recover()` under its shared lock. It may
restart only `cloudflared.service` after PR14's durable journal and readiness
checks. A compromised web process can request repeated recovery attempts; it
cannot supply a config mutation, destination, executable, service verb, or unit.
The root administrator, root-owned installed release, sudoers policy, and
existing cloudflared unit are trusted. The web service account must not own or
write the installed helper, release, sudoers file, adopted config, its parent,
or journal. Sudo inherits the web service's read-only `/etc/cloudflared` mount;
the transient root service gets a separate, fixed writable recovery mount view.
The web process cannot gain direct config write authority through later DAC or
ACL drift. If permissions change after installation, startup fails closed;
rerun bridge installation after correcting host ownership or modes.

For privileged host verification, run
`sudo visudo -cf /etc/sudoers.d/cloudflared-manager-bridge`, inspect ownership
and modes with `stat`, confirm the tmpfiles rule with
`cat /etc/tmpfiles.d/cloudflared-manager.conf`, and verify
`stat -c '%U:%G %a' /run/cloudflared-manager` reports `root:root 700`.
Confirm the web process sees `/etc/cloudflared`, `/etc/cloudflared-manager`,
and `/run/cloudflared-manager` read-only in its mount namespace,
and a recovery request starts a transient system service able to complete the
fixed PR14 transaction. Verify failed PR15 install/update leaves the prior
tmpfiles rule bytes and mode intact. On a PR14-to-PR15 migration host, verify
the candidate unit's pre-start bootstrap installed the fixed rule before the
first successful upgrade is reported, then reboot and confirm the `0700`
runtime directory is recreated before web startup.
Then send the version 1 recovery JSON through
`sudo -n -u cloudflared-manager /usr/bin/sudo -n /opt/cloudflared-manager/privileged-helper`
on a clean, adopted test host. A successful result has code
`NO_RECOVERY_REQUIRED`. Verify that adding an argument is denied and malformed
JSON returns `INVALID_REQUEST`. Before any real recovery, inspect the journal
as root and ensure the adopted `/etc/cloudflared/*.yml` path and
`cloudflared.service` match the PR14 contract. A `RECOVERY_REQUIRED` or
`ACTIVATION_FAILED` code means the operation did not establish success; retain
the journal and investigate. Automated tests use temporary config and fake
service boundaries and do not prove host sudo, systemd namespace, or
cloudflared integration.

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

Browser configuration mutation, DNS management, and managed-record ownership
tracking remain deferred to later reviewed changes. The production adoption
workflow is manager-configuration-only and strictly read-only toward
cloudflared; the internal service activation transaction is not exposed through
the dashboard or a supported activation CLI.

## Run tests

With the development dependencies installed:

```bash
python -m pytest
```

The tests use only in-process HTTP requests, deterministic fake command
runners, mocked subprocess calls, and temporary paths. They require no network
services, never invoke the machine's cloudflared or systemd, and never access
`/etc/cloudflared`.
