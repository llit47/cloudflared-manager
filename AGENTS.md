# Agent guidance for cloudflared-manager

These rules apply to the whole repository. This project is a small LAN-only web
application for managing services on an existing, locally managed Cloudflare
Tunnel. It replaces manual edits to `cloudflared` `config.yml`, DNS route
changes, configuration validation, and service restarts.

## Product and architecture

- Use Python with FastAPI, server-rendered Jinja templates, and HTMX where it
  helps. Avoid a heavy SPA unless its value is clear.
- Keep the existing `cloudflared` configuration authoritative for active
  ingress. Use SQLite only for manager-specific metadata and state; reconcile
  stored state with the actual configuration rather than treating the database
  as the source of active routes.
- Keep Cloudflare API access, config parsing and mutation, system service
  control, persistence, and HTTP/UI code separate. Put external side effects
  behind interfaces that tests can replace with fakes.
- Default the UI to LAN-only access. Do not expose the management UI through
  the tunnel or a public listener by default. Treat LAN access as a network
  boundary, not as a substitute for application safeguards.

## Maintainability and modularity

- Give each module one coherent responsibility. Avoid monolithic files, god
  objects, god modules, and route handlers that accumulate unrelated work;
  split modules when their responsibilities diverge or they become difficult
  to understand and maintain.
- Keep HTTP routing, presentation/view models, cloudflared parsing, Cloudflare
  API access, service control, persistence, and orchestration as separate
  concerns. Business and infrastructure logic must not accumulate in FastAPI
  route functions or Jinja templates.
- Prefer small, composable components with explicit inputs and outputs. Keep
  components with external side effects replaceable and independently
  testable.
- Do not overcorrect with dozens of meaningless tiny files or speculative
  abstraction layers. Add a boundary when it supports a real responsibility.
- Optimize architecture for future debugging, replacement, extension, and
  targeted testing. New features should usually be possible without changing
  unrelated subsystems.

## UI and UX

- Build a polished, modern, clean, visually consistent interface rather than a
  generic admin prototype. Keep it lightweight and primarily server-rendered
  with Jinja and HTMX; appearance alone does not justify a heavy SPA, large UI
  framework, or icon library.
- Make the UI responsive and usable on desktop and mobile. Support automatic
  light and dark themes via `prefers-color-scheme`; design both intentionally
  with strong contrast and readable status indicators.
- Use semantic CSS variables/design tokens for colors, surfaces, borders, text,
  success/warning/error states, spacing, and other shared visual primitives
  so both themes remain consistent.
- Show service status and dangerous actions with text or icons/labels as well
  as color. Make Delete visually distinct and require confirmation.
- Keep Add, Enable, Disable, Edit, and Delete easy to find without clutter.
  Make tunnel, `cloudflared`, config health, and managed-service status clear
  at a glance on the primary dashboard.
- Favor accessibility, keyboard use, visible focus states, and sensible touch
  targets. Avoid excessive animation, unnecessary effects, and dashboard
  clutter.

## Service behavior

- **Add:** create an ingress rule and its required, explicitly managed DNS
  record.
- **Disable:** remove or deactivate the active ingress rule while retaining
  enough local metadata and DNS state to enable it again easily.
- **Enable:** restore the ingress rule without duplicating it or changing
  unrelated records.
- **Edit:** safely change hostname, origin, protocol, or port, reconciling
  config and managed DNS as needed.
- **Delete:** after explicit confirmation, remove the ingress rule, associated
  manager state, and managed DNS record. Never infer that an unrelated record
  is manager-owned.

## Safety boundaries

- Development and automated tests MUST NOT modify the real
  `/etc/cloudflared/config.yml`. Use fixtures, temporary files, or an
  explicitly configured test path; keep tests isolated from real DNS and
  service-control operations.
- Never commit Cloudflare API tokens, tunnel credentials, secret-bearing account
  IDs, `.env` files, certificates, production configuration, or other real
  credentials. Use least-privilege API tokens and OS permissions.
- Never execute shell commands supplied by the UI. Implement system actions as
  explicit allowlisted operations with controlled arguments; do not turn user
  input into command text.
- Treat production systemd/root/sudo access as a separately configured
  deployment boundary. Do not assume it in development, and avoid running
  Codex or the application as unrestricted root.
- Safeguard destructive UI actions with explicit confirmation and server-side
  checks. Limit DNS mutation to records positively identified as managed by
  this application; leave all other zone records alone.
- Preserve the terminal catch-all ingress rule (for example,
  `http_status:404`). Place hostname rules before it, and preserve unrelated
  ingress rules and config fields. Reject ambiguous or unsafe mutations rather
  than guessing ownership.
- Make configuration changes transactional where practical: read the current
  file, make a backup, write a candidate safely, validate it with
  `cloudflared`, then activate/restart and verify health. On activation failure,
  restore the backup and previous working service state automatically. Handle
  partial DNS/config failures with explicit compensation or a recoverable
  reconciliation state; never report partial success as complete.

## Privileged cloudflared activation

- Follow `docs/activation-transaction.md` for any work that can replace the
  adopted cloudflared config or control `cloudflared.service`. Treat its
  non-negotiable invariants as hard requirements, not implementation advice.
- Keep the long-running web service non-root and unable to write the adopted
  config, its directory, privileged transaction state, or service-control
  interfaces. LAN-only access is not privileged authorization.
- Derive the active target only from the root-owned explicitly adopted setting.
  Never mutate a detected-only path or accept an active, backup, journal,
  executable, or service path from browser input.
- Revalidate the adopted path, active manager release, source bytes and
  file/parent identity immediately before commit. Reject concurrent or manual
  changes; never merge or rebase a prepared mutation onto changed config.
- Preserve PR10's bounded/no-follow snapshots, round-trip structural checks,
  terminal catch-all, exclusive `0600` staging, fsync, retained candidate
  identity, layered parsing, and FD-bound cloudflared validation. Root privilege
  does not permit bypassing them.
- Never truncate the active config in place. Require exact durable backup,
  intended metadata handling, race-aware same-filesystem atomic commit, file
  and directory fsync, and authenticated crash recovery before production
  activation is enabled. Unsupported ACLs/xattrs or ambiguous filesystem state
  fail closed.
- Require a verified healthy, stable cloudflared service baseline before active
  config mutation; an initially inactive, failed, mismatched, or unstable
  service fails closed. Treat command completion, state, process identity,
  readiness, and stability as separate checks.
- After durable commit intent, freshly recheck source/candidate/adopted identity
  and the complete service baseline immediately before exchange. A proven
  pre-exchange failure aborts without config or service mutation; once the
  active name may have changed, use rollback. Do not claim process liveness and
  filesystem exchange are atomic.
- Record a durable commit-or-rollback cleanup decision before deleting recovery
  artifacts, then make authenticated cleanup idempotent and directory-fsynced.
  A failed activation is never success; distinguish verified rollback from
  partial or failed rollback.
- Under the shared outer manager lock, treat every valid nonterminal or
  unverifiable activation journal as a persistent recovery barrier. Update,
  release reconciliation, adopt, and clear must reject without mutation until
  privileged recovery reaches a valid terminal state; recovery takes the same
  outer lock.
- Before update, release reconciliation, adopt, or clear changes authority,
  authenticate any terminal activation journal against the current authority,
  securely retire it, and fsync its directory under that lock. Retirement
  failure aborts the authority change; never ignore a terminal identity
  mismatch.
- Keep privileged operations allowlisted with fixed executable/service
  identities, strict bounded inputs, fixed argv, bounded timeouts,
  `shell=False`, minimal environment, and sanitized errors. Never create a
  generic root command, file-copy, YAML-path, or systemctl proxy.
- Do not combine local config activation with DNS/API ownership or expose a web
  mutation surface until each boundary has its own reviewed design and tests.

## Engineering workflow

- Keep modules small and responsibilities clear. Avoid dependencies without a
  clear benefit, unrelated refactors, and silent scope expansion.
- Add meaningful, deterministic tests for config mutation, catch-all ordering,
  rollback, add/edit/enable/disable/delete semantics, ownership boundaries,
  and dangerous edge cases. Use temporary directories and mocked external
  services. Run relevant tests and checks before declaring work complete.
- Update documentation when operational behavior changes. Report changes,
  tests run, and remaining uncertainty clearly.
- Do not develop features directly on `main`; create a focused branch for each
  meaningful unit of work. Keep commits logically scoped. Inspect `git diff`
  and `git status` before committing. Never commit secrets or production config,
  force-push, or merge a PR without explicit user instruction.
- PR descriptions should explain behavior, tests, risks, and deployment
  implications.
