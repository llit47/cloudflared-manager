# PR15 privileged bridge

## Reconnaissance and boundary

The merged PR12–PR14 code provides `FilesystemActivation.run(mutation,
validator=...)` and `FilesystemActivation.recover()` in
`activation/transaction.py`. `run` prepares its own candidate, validates it,
commits it, restarts the fixed `cloudflared.service`, verifies readiness, and
performs durable rollback or recovery. Both methods require root and use the
same `DeploymentLock` as root manager administration. `DeployedAuthority`
derives the adopted path from the root-owned environment and checks the active
release. No production HTTP or CLI caller invokes either method today.

The only existing mutation parameter is a Python callable over an editable
document. It cannot safely cross a JSON/sudo boundary. The present editing
layer implements one insertion primitive, but the project has no reviewed
serializable hostname lifecycle operation or DNS ownership model. PR15 must
not serialize a callable, accept YAML, or offer arbitrary path or command
operations. A later reviewed domain operation can use the same privileged
entrypoint and existing `run` transaction.

The production manager runs as `cloudflared-manager` under
`deploy/cloudflared-manager.service`. Installation, update, adoption, and
release switching are root administrator operations. The manager service is
currently configured with `NoNewPrivileges=true` and
`RestrictSUIDSGID=true`, which prevent a setuid sudo transition. The PR15
deployment change must explicitly address that interaction while preserving
the remaining sandbox and limiting sudoers to one root-owned executable with
no arguments.

The root-owned immutable release and stable administration launcher pattern in
`deployment/release.py` and `deploy/config.sh` supplies the model for the
helper's fixed entrypoint. The caller will send one size-bounded, versioned
JSON document over stdin using `sudo -n` and fixed argv. The privileged process
will validate the schema independently, construct fixed production paths, and
return a bounded sanitized JSON result. A compromised manager account can
invoke only the operations admitted by that schema and sudoers policy.

## Components to change

- Add protocol parsing, root helper dispatch, and a non-root sudo client in
  `src/cloudflared_manager/activation/`.
- Add a root-owned stable helper launcher and exact sudoers asset under
  `deploy/`; extend deployment path, release, and installer validation to
  install and verify them.
- Adjust the manager unit only as needed for `sudo -n` to cross its existing
  service sandbox.
- Add deterministic protocol, client, helper, deployment, and security tests;
  keep PR12–PR14 activation tests intact.
- Update the activation contract and README to describe only installed
  capabilities and manual privileged verification.
