# PR15 privileged bridge

## Reconnaissance and implemented boundary

The merged PR12–PR14 code provides `FilesystemActivation.run(mutation,
validator=...)` and `FilesystemActivation.recover()` in
`activation/transaction.py`. `run` prepares its own candidate, validates it,
commits it, restarts the fixed `cloudflared.service`, verifies readiness, and
performs durable rollback or recovery. Both methods require root and use the
same `DeploymentLock` as root manager administration. `DeployedAuthority`
derives the adopted path from the root-owned environment and checks the active
release. No production HTTP or CLI caller invokes `run`.

The only existing mutation parameter is a Python callable over an editable
document. It cannot safely cross a JSON/sudo boundary. The present editing
layer implements one insertion primitive, but the project has no reviewed
serializable hostname lifecycle operation or DNS ownership model. The version
1 protocol therefore exposes only `recover`. It never accepts YAML, paths,
executables, shell text, unit names, or serialized callables. A later domain
transaction requires a separate review before it can use `run`.

The production manager runs as `cloudflared-manager` under
`deploy/cloudflared-manager.service`. Installation, update, adoption, and
release switching are root administrator operations. The manager service is
previously configured `NoNewPrivileges=true`, which prevented a setuid sudo
transition. PR15 sets it to `false`, keeps `RestrictSUIDSGID=true`, and limits
the capability bounding set and writable mount paths to those needed by the
bridge. Sudo inherits those mount paths, so root recovery needs them writable.
Before bridge installation, root checks the complete bounded
`/etc/cloudflared` tree for unsafe ownership, modes, ACLs, and objects. The
check rejects every visible `system.*` xattr, including POSIX default ACLs
on directories, so newly staged transaction objects cannot inherit a grant
to the web account. A visible unsupported ACL or unreadable metadata fails
closed. The adoption path repeats that check; the production web entry point
checks effective write access at startup. The adopted file cannot belong to the web
UID, and no object in the tree can grant it direct write access. The root-owned
`/etc/cloudflared-manager` and `/run/cloudflared-manager` directories must
remain `0750` and `0700`, respectively; root verifies the private environment
file is `0600`. Unsafe state blocks installation, adoption, or startup.
`CAP_SYS_PTRACE` is included because PR14 reads
`/proc/<MainPID>/environ` and executable identity when cloudflared may run
under a different UID; DAC override alone does not pass that ptrace read
check. The service user still has no ambient capabilities and cannot write
the root-owned release, config, journal, helper, or sudoers file by DAC.

The root-owned immutable release and stable administration launcher pattern in
`deployment/release.py` and `deploy/config.sh` supplies the model for the
helper's fixed entrypoint. The client sends one size-bounded, versioned JSON
document over stdin using fixed `sudo -n` argv. The helper validates the schema
independently and returns a sanitized JSON result. A compromised manager
account can repeatedly request recovery but cannot supply a config mutation
or select another privileged action.

## Components

- `activation/bridge_protocol.py` strictly parses one version 1 request.
- `activation/bridge_client.py` invokes fixed `/usr/bin/sudo -n` argv.
- `activation/bridge_helper.py` dispatches only `recover()` and sanitizes
  results.
- `deploy/privileged-helper.sh` enters Bash privileged mode, invokes the sole
  pre-sanitization external command as `/usr/bin/readlink`, and launches the
  active root-owned release with `python -I` and a fixed minimal environment.
- `deploy/cloudflared-manager-bridge.sudoers` grants the exact helper path;
  `cfm-config install-bridge` validates and installs both assets explicitly.
- `deploy/cloudflared-manager.service` permits the sudo transition within a
  bounded capability and mount namespace.
- `deploy/cloudflared-manager.tmpfiles.conf` recreates root-owned `0700`
  `/run/cloudflared-manager` through systemd-tmpfiles at boot. Install, update,
  reconciliation, and bridge installation apply the fixed rule immediately
  and reject unsafe pre-existing runtime metadata before applying it.
- Deterministic tests use fake service boundaries and temporary paths. Manual
  host verification is described in the README.
