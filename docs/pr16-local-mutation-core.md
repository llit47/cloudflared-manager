# PR16 local ingress mutation foundation

PR16 adds an internal, privileged foundation for local cloudflared hostname
ingress Add, Edit, and Delete. The browser remains read-only: there are no HTTP
mutation routes, authentication, sessions, or CSRF handling in this release.
Product-level Add/Edit/Delete still require managed DNS ownership and
reconciliation; those semantics are not implemented here. Enable/Disable,
Cloudflare API access, DNS changes, and manager persistence remain unsupported.

## Authority and targeting

The unprivileged client sends only a versioned, bounded JSON domain request to
`/opt/cloudflared-manager/privileged-mutation-helper`. It contains the action,
the SHA-256 digest of the exact observed source bytes, a bounded hostname/path/
service value where needed, and a position plus route-projection fingerprint
for Edit/Delete. The position selects an entry only after the privileged
process safely snapshots the root-adopted source, checks its digest, and
verifies the selected hostname rule's fingerprint. Duplicate-looking routes
are distinguishable by position in that same source revision. Mismatches fail
closed; there is no route search or rebase.

The request cannot supply YAML, a configuration or artifact path, a command,
an executable, a service unit, or an environment. The privileged process
derives the active target from the root-owned adopted setting and confines it
to `/etc/cloudflared`. It converts the validated request to an internal
document mutation, preserving unrelated YAML fields where round-trip editing
supports them. It then calls the existing `FilesystemActivation.run(...)`
with the fixed cloudflared validator. PR12–PR14 candidate, backup, journal,
exchange, restart, readiness, rollback, and recovery checks remain in that
transaction. A second identical request fails the source revision check after
the first commit.

The response contains only a bounded code. It distinguishes changed, no
change, stale conflict, invalid input, busy lock, unsupported structure,
validation failure, unavailable privileged boundary, verified rollback, and
recovery-required or indeterminate activation. It never returns YAML,
submitted values, paths, commands, stdout/stderr, or tracebacks.

## Explicit installation and migration

The PR15 recovery helper and `/etc/sudoers.d/cloudflared-manager-bridge`
remain recovery-only. PR16 adds a separate root-owned launcher and exact
no-argument sudoers policy at
`/etc/sudoers.d/cloudflared-manager-mutation-bridge`. A root administrator
must explicitly run:

```bash
sudo cfm-config install-mutation-bridge
```

The command checks that its release is still current, takes the existing
nonblocking deployment lock, checks the activation recovery barrier and write
boundary, validates fixed assets and sudoers syntax, establishes the existing
tmpfiles runtime state, and installs the helper and policy with reverse-order
rollback on failure. Incomplete rollback is reported separately. It is
idempotent.

An ordinary install, update, or reconciliation does not call this installer
or write the mutation sudoers rule. During an upgrade from PR15, the old
active updater runs until the release switch; that updater has no mutation
installer. The new release's unit pre-start bootstrap only maintains the
existing runtime tmpfiles rule. The existing PR15 helper and sudoers grant
still dispatch recovery only, so upgrading a host with the PR15 bridge does
not grant mutation authority. A previously explicit PR16 mutation grant is a
separate administrator choice and persists across later compatible upgrades.

The long-running web unit stays non-root, `ProtectSystem=strict`, without
privileged writable paths or ambient capabilities. The separate launcher uses
fixed sudo argv, Bash privileged mode, fixed systemd-run arguments, a minimal
environment, `python -I`, bounded execution, and only the already required
activation writable paths. It does not change the recovery bridge.

Future HTTP mutation routes need a separate authentication, authorization,
CSRF, confirmation, and DNS ownership review before exposure.
