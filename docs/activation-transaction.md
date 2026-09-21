# Privileged cloudflared activation transaction

## Status and purpose

This document is the security and engineering contract for a future
Cloudflared Manager activation implementation. It is a design specification,
not a description of capability that exists today. Cloudflared Manager remains
operationally **READ-ONLY**: no current web, CLI, deployment, or editing path
activates a candidate, changes DNS, or controls `cloudflared.service`.

The design starts from the candidate-only foundation introduced in PR10. That
foundation snapshots an adopted source, performs a narrow round-trip YAML
mutation, stages a separate candidate in the source directory, and validates
the staged identity with both the application parser and cloudflared. It stops
before replacing the active file.

The future transaction has one purpose: change the one explicitly adopted
cloudflared configuration through a narrow privileged boundary, and either
prove the new configuration and service are working or restore and prove the
previous working state. Convenience is subordinate to failing safely.

The words **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are normative. Sections
labelled "Proposed implementation" are the preferred implementation subject to
the review gates and unresolved decisions at the end of this document.

## Scope and non-goals

This design covers:

- the actors and privilege boundary;
- source and candidate identity validation;
- stale-write rejection;
- a durable backup and filesystem commit;
- cloudflared service activation and readiness verification;
- rollback, crash recovery, locking, and safe error reporting; and
- an adversarial test contract for later implementation PRs.

This documentation PR does **not** implement any part of that transaction. It
must not add production writes, sudoers policy, a privileged helper, service
control, HTTP mutation routes, enabled Add/Edit/Delete operations, Cloudflare
API access, or DNS mutation.

The activation transaction itself also excludes DNS and Cloudflare API work.
It changes one local configuration file and, in the service phase, activates
that local configuration. Ownership of DNS records and compensation across DNS
and local configuration require a separate design.

## Terminology

- **Adopted path**: the canonical absolute cloudflared YAML path explicitly
  persisted by the root-only adoption workflow. A merely detected path is not
  adopted.
- **Source snapshot**: the immutable PR10 record of source bytes, SHA-256,
  size, device/inode, UID/GID/mode, timestamps, and parent identity used to
  prepare a candidate.
- **Candidate**: the exclusively created, `0600`, fsynced, same-directory file
  containing a controlled mutation. It is not active merely because it passed
  validation.
- **Active name**: the adopted directory entry cloudflared is configured to
  read.
- **Backup**: a durable, restrictive copy of the exact pre-transaction active
  bytes plus authenticated restoration metadata. It is not a YAML re-render.
- **Transaction journal**: the minimal root-owned durable recovery record for a
  transaction that has not yet reached a fully cleaned final outcome. It
  identifies expected source, candidate, backup, cleanup allowlist, and phase;
  it is not historical storage and must not contain raw YAML or credentials.
- **Published journal**: the single fixed `journal` leaf. Only this durably
  published leaf is transaction/recovery authority.
- **Staged journal**: the single fixed `journal.next` leaf used to construct the
  next journal generation. It is never authority and recovery never promotes
  it.
- **Durably clean journal namespace**: the verified condition, established
  under the shared outer lock, that the fixed root-owned journal directory has
  retained its fixed-path/parent identity, has been fsynced, and has then been
  rechecked through the same descriptor with neither `journal` nor
  `journal.next` nor any unknown journal-like object present.
- **Activation**: both committing the candidate at the active name and, when
  service integration exists, making cloudflared run from it and proving
  readiness.
- **Sanitized result**: a bounded result containing an allowlisted status/code
  and non-secret facts, never raw YAML, paths, command lines, stdout, or stderr.

## Security model and trust boundaries

### Long-running web application

The FastAPI service runs as the dedicated, unprivileged
`cloudflared-manager` identity. It is not trusted with arbitrary host mutation.
It MUST remain non-root, outside privileged cloudflared/root-writable groups,
and unable to write the adopted config, its directory, root-owned backup or
journal storage, systemd units, or privileged executables.

It MUST NOT receive broad sudo rights or a generic command interface. Future
validated domain input may request an allowlisted operation, but browser text,
paths, shell fragments, executable names, environment variables, and systemd
unit names MUST NOT become privileged command construction.

### Root administrator

The root administrator owns the deployment and administrative trust boundary.
The administrator explicitly adopts a path and explicitly installs or enables
any future write capability. Detection does not imply adoption, and an upgrade
must not silently enable write capability.

Root is trusted, but concurrent manual administration is still expected. A
manual edit racing the manager is legitimate interference and MUST cause stale
state rejection or a distinct rollback outcome rather than silent overwrite.
No design can protect against a deliberately malicious root; the goal is to
avoid losing independent administrative work and to detect namespace races.

### Narrow privileged activation component

The future component runs with only the privilege needed for one allowlisted
transaction. It MUST NOT be a general root command runner, YAML editor, file
copy service, arbitrary path writer, systemctl proxy, or reusable shell
facility. Running as root increases its validation obligations; it does not
allow it to bypass PR10 checks.

The component independently derives and validates authority. It MUST NOT trust
the caller's claim about the adopted path, source identity, candidate identity,
active release, or service name.

### Existing cloudflared configuration and service

The adopted cloudflared config remains authoritative for active ingress.
Cloudflared and its config may also be administered outside Cloudflared Manager.
The transaction cannot reliably lock out root or independent operator tools,
so it combines a manager lock with immediate stale-state checks and a
race-aware commit protocol.

`cloudflared.service` is a separate service from
`cloudflared-manager.service`. Permissions or lifecycle control for one MUST
NOT be generalized to the other.

### Browser and client

The browser is untrusted. LAN-only access is **not** authorization for
privileged filesystem or service actions. A future HTTP authorization and CSRF
design is required before exposing mutations, even after a privileged helper
exists. Validated domain objects, not request strings, must cross application
boundaries.

## Non-negotiable invariants

Future implementation MUST preserve all of the following:

1. The long-running web service never gets generic write access to the adopted
   cloudflared config or its directory.
2. The long-running web service never runs as root and is not added to a
   privileged group for convenience.
3. Privileged activation is a narrow, allowlisted boundary with no arbitrary
   command, path, environment, file-copy, or systemctl surface.
4. Only the explicitly adopted cloudflared config may be considered for
   mutation.
5. A detected-but-unadopted path can never be mutated.
6. The adopted path and the current immutable manager release are revalidated
   inside the privileged boundary at transaction time. Earlier validation is
   insufficient.
7. The exact source state used to prepare the candidate must still be current
   at commit time.
8. A stale source, concurrent/manual change, path or parent identity change,
   symlink substitution, or unsupported metadata change causes fail-closed
   rejection.
9. A prepared candidate is never automatically rebased or merged into newly
   changed operator configuration.
10. The active file is never partially written, opened for writing, or
    truncated in place.
11. PR10 candidate validation remains bound to the staged directory identity
    through `/proc/self/fd/<dirfd>/<candidate-name>`, explicit `pass_fds`, and
    `shell=False`; path-only validation is forbidden.
12. The terminal ingress catch-all remains final and all unrelated YAML data is
    preserved to the extent guaranteed by the round-trip editing layer.
13. A semantic no-op does not stage, back up, commit, restart, or create a
    transaction journal.
14. The backup represents the exact active pre-transaction bytes and records
    the metadata required for exact intended restoration.
15. Rollback restores the intended previous bytes and metadata; it does not
    regenerate equivalent YAML.
16. Activation failure is never reported as success, including when rollback
    succeeds.
17. Activation failure with verified rollback is distinguishable from rollback
    failure, service-recovery failure, and indeterminate crash recovery.
18. DNS and Cloudflare API operations are outside this transaction.
19. Browser-safe errors and logs contain no raw YAML, secrets, credentials,
    local paths, raw command output, or caller-provided values.
20. No shell command is built from UI input. External commands use fixed argv,
    fixed executable identity, bounded timeouts, `shell=False`, a minimal fixed
    environment, and bounded captured output.
21. Candidate, backup, journal, and lock names are internally generated or
    fixed, never chosen by a browser/client.
22. Every privileged filesystem operation is directory-relative through
    verified descriptors where Linux permits it; pathname checks alone are not
    considered stable authority.
23. A later privileged implementation must not modify sudoers, systemd policy,
    or service permissions as an incidental side effect. Enabling the boundary
    is a separately reviewed administrator action.
24. Production activation requires a verified healthy and stable
    `cloudflared.service` baseline before active namespace mutation. An
    initially inactive, failed, mismatched, or unstable service is an
    unsupported precondition, not a condition the transaction repairs.
25. Rollback/recovery artifacts are never deleted before a durable final
    cleanup decision. Cleanup after that decision is authenticated, idempotent,
    directory-fsynced, and followed by journal retirement, journal-directory
    fsync, and clean-namespace verification before a user-visible result.
26. After durable commit intent, the source, candidate, adopted authority, and
    complete service baseline are freshly rechecked immediately before
    exchange. A proven pre-exchange failure aborts without active-config or
    service mutation; once exchange may have occurred, failure requires
    rollback rather than precommit cleanup.
27. A persistent activation-recovery barrier protects authority after process
    locks are released. Under the shared outer manager lock, every root manager
    mutation must reject a valid recovery-required or unverifiable journal
    before changing the active release, adopted path, or related manager state.
    It may proceed only after establishing a durably clean journal namespace;
    pathname absence alone is insufficient. Any journal left after a final
    cleanup decision is a recovery artifact to retire, never permanent history.
28. Only the fixed, durably published `journal` leaf is recovery authority.
    Fixed staging leaf `journal.next` is never authority and is never promoted
    by recovery. Unknown journal-like namespace objects fail closed.
29. Every journal phase update uses the same staged-publication protocol. No
    filesystem or service side effect that depends on the successor phase may
    begin until `journal.next` has replaced `journal`, the journal directory has
    been fsynced, and the published successor has been reopened and verified.
    Initial journal publication is durable before any active-config, service,
    rollback, or cleanup mutation is permitted.

## Recommended privileged boundary

### Options considered

**Run FastAPI as root or grant broad directory writes.** Rejected. Both erase
the intended boundary and turn an application or template/HTTP bug into general
cloudflared configuration compromise.

**Generic sudo command or systemctl proxy.** Designs such as
`sudo cloudflared-manager *`, wildcard sudoers arguments, arbitrary helper
paths, or a command string passed through a shell are rejected. Argument
matching is not domain authorization, and such interfaces are difficult to
constrain as features grow.

**Permanent privileged daemon.** Not preferred for the first implementation.
It creates a long-lived parser and IPC attack surface, lifecycle concerns, and
an additional privileged state machine. FD passing over a Unix socket could be
useful later, but requires a demonstrated need and separate threat review.

**Transient privileged systemd operation.** Potentially credible, especially
for resource and filesystem sandboxing, but it still needs an authenticated,
bounded request transport and authorization to start the unit. Passing dynamic
properties or command lines would recreate a generic privileged interface.
This remains an alternative if a static template/service plus root-owned spool
is demonstrably smaller than a direct helper.

**Small root-owned helper with an exact interface.** Preferred for the first
implementation, initially invoked only by a root administrator. It fits the
existing root-owned immutable-release and `cfm-config` architecture, can reuse
the existing release-identity check, and need not add a daemon or any web
surface. A later web-to-helper authorization mechanism is a separate review.

### Proposed helper contract

The first helper MUST be installed from an immutable, root-owned manager
release and invoked by the existing root administrative boundary. It performs
one verb: prepare/commit or recover one adopted-config transaction. It MUST NOT
accept the active path, backup path, journal path, executable path, service
unit, shell text, or arbitrary YAML path as command-line arguments.

The helper obtains the adopted path from the root-owned manager EnvironmentFile
using the existing strict parser, then revalidates that it is canonical and is
the sole configured adopted path. It verifies that its embedded release ID
matches the root-owned `current` release before any mutation, using the same
stale-process principle as existing `cfm-config` operations. The executable
and stable launcher are root-owned and not writable by the service identity.

For the filesystem-foundation PR, the exact caller is a root administrator and
there is no sudoers grant to the web service. A future web integration MUST add
a separately reviewed authorization/transport design. It may invoke one exact
helper command, but MUST NOT rely on wildcard command-line matching. The helper
must independently verify the active manager release and adopted path.

Only a versioned, size-bounded, strict domain request may eventually cross the
boundary. It may contain an operation discriminator and validated domain fields
needed by a narrow mutation. It must reject unknown keys, unknown versions,
duplicate fields, NULs, oversized values, and trailing data. It does not carry
paths, raw YAML, command names, environment assignments, backup names, service
names, or serialized Python objects. The privileged side reruns snapshot,
round-trip mutation, candidate staging, application validation, and FD-bound
cloudflared validation rather than trusting a caller-created candidate.

The helper environment is constructed from an allowlist, with a fixed safe
`PATH` or absolute executable paths, fixed locale, restrictive umask, and no
caller-controlled Python/import, loader, proxy, cloudflared, or systemd
variables. Standard input/output are bounded. Results use stable sanitized
codes; diagnostics with sensitive subprocess output are retained only if a
future root-only logging policy explicitly permits it.

No sudoers syntax is specified here. The first implementation does not need
one. Before a later web bridge is enabled, review must settle the exact request
transport, invocation path, caller authentication, rate/concurrency control,
and how the active web process/release is bound to the request.

## Transaction inputs and derived authority

A transaction begins with these independently derived inputs:

- the adopted path read from root-owned manager configuration;
- the active immutable manager release identity;
- a narrow validated domain operation;
- PR10 source snapshot and controlled candidate objects created inside the
  privileged process;
- fixed root-owned lock, journal, and backup locations; and
- fixed cloudflared and, in the service phase, systemctl executable/unit
  identities.

No caller-provided absolute path is authoritative. A digest supplied by a
caller may be used as an additional optimistic precondition, never as a
substitute for privileged snapshotting and revalidation.

## Transaction state machine

The journal uses a versioned subset of the following states. In-memory states
before durable backup need not all be journaled, but transitions and results
must retain these semantics.

```text
IDLE
  -> SOURCE_VERIFIED
  -> CANDIDATE_PREPARED
  -> CANDIDATE_VALIDATED
  -> SERVICE_BASELINE_VERIFIED
  -> PRECOMMIT_REVALIDATED
  -> BACKUP_DURABLE
  -> CONFIG_COMMITTING
  -> PRE_EXCHANGE_REVALIDATED
  -> CONFIG_COMMITTED
  -> SERVICE_ACTIVATING
  -> SERVICE_VERIFIED
  -> COMMIT_CLEANUP_PENDING
  -> DURABLY_CLEAN_JOURNAL_NAMESPACE
  -> COMMITTED_SUCCESS (logical result only)

Failure after durable recovery state exists but before any active-name change:

BACKUP_DURABLE or CONFIG_COMMITTING
  -> PRECOMMIT_ABORT
  -> DURABLY_CLEAN_JOURNAL_NAMESPACE
  -> FAILED_PRECOMMIT (logical result only)

Failure after the active name changed or may have changed:

ACTIVATION_FAILED
  -> ROLLBACK_CONFIG
  -> ROLLBACK_SERVICE
  -> ROLLBACK_VERIFIED
  -> ROLLBACK_CLEANUP_PENDING
  -> DURABLY_CLEAN_JOURNAL_NAMESPACE
  -> FAILED_ROLLED_BACK (logical result only)

Any unverified restoration or service recovery:

ROLLBACK_FAILED
```

`CONFIG_COMMITTING` is included because a crash can occur during the atomic
namespace operation before a later journal update. `BACKUP_DURABLE` is the last
state that is still purely read-only with respect to the active name. The
atomic exchange/replace that leaves candidate bytes at the active name is the
point of no longer purely read-only staging.

`PRE_EXCHANGE_REVALIDATED` is the fresh, in-memory result of rechecking the
source, candidate, adopted authority, and complete healthy service baseline
after the `CONFIG_COMMITTING` generation has completed the fixed-leaf
publication protocol. No journal publication, filesystem preparation, service
operation, or other deliberate work occurs between that final observation and
the namespace exchange. If the check already differs and the active name is
still provably the exact original source, the transaction publishes
`PRECOMMIT_ABORT` without touching the active name or controlling cloudflared.

This does not create an impossible atomic guarantee. Process liveness and a
filesystem exchange cannot be atomically coupled: cloudflared may exit or
change state immediately after the last observation. If the baseline changes
after the final check, during the exchange, or after it, post-commit service
verification detects the mismatch and treats the transaction as activation
failure requiring rollback. `PRE_EXCHANGE_REVALIDATED` narrows the race; it
does not claim to eliminate it.

`SERVICE_BASELINE_VERIFIED` is a mandatory precommit gate, not merely an
observation for diagnostics. The first production implementation accepts only
a loaded, active, stably running cloudflared service whose process/executable
identity and relationship to the adopted config are verified. An inactive,
failed, activating, deactivating, restart-looping, path-mismatched, or otherwise
unhealthy service causes rejection before `CONFIG_COMMITTING`; the transaction
does not attempt to turn that state into a supported baseline.

`COMMIT_CLEANUP_PENDING` and `ROLLBACK_CLEANUP_PENDING` are durable terminal
decisions, but they are still recovery journal states. Once either has been
published and reverified, the selected outcome no longer depends on rollback
artifacts remaining present. Cleanup can then remove authenticated artifacts
idempotently. User-visible success or verified-rollback failure is not returned
until cleanup and all affected-directory fsyncs complete, the journal is
securely unlinked, its directory is fsynced, and the journal namespace is
rechecked as clean.

A cleanup error after either durable decision does not reverse that decision.
Before journal unlink, the journal remains in its cleanup-pending state, no new
transaction may begin, and root recovery resumes the same idempotent cleanup.
Once unlink has been attempted, the journal may appear present or absent, but
the lifecycle remains open until its directory is fsynced and absence is
rechecked. An existing artifact with the wrong identity is not treated as
already cleaned and requires manual intervention; an allowlisted artifact that
is absent is safe to skip only with the required directory fsync.

`PRECOMMIT_ABORT` is also a durable cleanup decision, but it is not rollback.
It is reachable from `BACKUP_DURABLE` when recovery or a later precondition
cannot proceed, and from `CONFIG_COMMITTING` after a failed final recheck. Both
paths are permitted only after proving the active name still identifies the
exact original source. The state authorizes idempotent deletion of only the
journaled candidate, backup, and transaction artifacts. It never changes the
active config or invokes cloudflared service control. It remains the durable
journal decision until cleanup, journal retirement, journal-directory fsync,
and clean namespace verification finish.

After the active name changed or may have changed, the helper publishes and
reverifies `ACTIVATION_FAILED` before beginning rollback. Each subsequent
rollback intent is likewise published before the config or service side effect
it authorizes. If any such publication is interrupted, the still-published
predecessor phase remains sufficient to classify the active config/service
state and retry safely; `journal.next` never authorizes rollback.

`COMMITTED_SUCCESS`, `FAILED_PRECOMMIT`, and `FAILED_ROLLED_BACK` are logical
result states, not persistent journal phases. They may be returned and captured
by a separately secured audit mechanism only after
`DURABLY_CLEAN_JOURNAL_NAMESPACE` is established. A transaction journal is
intentionally retained only while transaction recovery, cleanup, or durable
retirement work may remain; it is never retained to remember history. An
interrupted unlink can make the name appear absent while retirement durability
is still unproven, which is why observed absence never closes the lifecycle by
itself.

### State and transition contract

Every phrase below that publishes, records, or advances a journal phase means
the complete fixed-leaf protocol, including `journal.next` fsync, atomic rename,
journal-directory fsync, and published `journal` reverification. The dependent
side effect in that row begins only afterward.

| Transition | Prerequisites and verification | Side effects and durable state | Failure behavior |
| --- | --- | --- | --- |
| `IDLE -> SOURCE_VERIFIED` | Shared outer lock held; durably clean journal namespace established; helper/release and adopted path independently valid; canonical path opened without symlinks; bounded snapshot succeeds | Read-only descriptors and immutable snapshot only | Close descriptors; sanitized rejection; no rollback |
| `SOURCE_VERIFIED -> CANDIDATE_PREPARED` | Narrow mutation accepts structure and reports a real change | Exclusive random `0600` same-directory candidate, complete write and candidate file `fsync`; retained file/directory identity | Remove only the verified candidate; source untouched |
| `CANDIDATE_PREPARED -> CANDIDATE_VALIDATED` | Candidate identity intact | Existing application parser succeeds, then fixed cloudflared ingress validation succeeds through FD-bound path; sanitized report retained | Discard candidate; source untouched |
| `CANDIDATE_VALIDATED -> SERVICE_BASELINE_VERIFIED` | Unit is loaded; state is active with the expected running substate; positive MainPID, process start identity, executable identity, adopted-config relationship, and readiness remain stable across a bounded observation | Read-only checks only; sanitized baseline facts retained in memory | Reject before active mutation; discard candidate; do not start/restart/reload an unhealthy service |
| `SERVICE_BASELINE_VERIFIED -> PRECOMMIT_REVALIDATED` | Baseline is still current; candidate rechecked; adopted setting reread; active path/parent reopened through no-follow descriptor walk; source bytes and metadata compared with original snapshot | Read-only checks only | Reject stale source or service state; never merge/rebase; discard candidate |
| `PRECOMMIT_REVALIDATED -> BACKUP_DURABLE` | Source still bound by retained descriptors; service baseline remains valid; candidate contents and metadata conversion complete; candidate file and verified active/candidate parent directory fsynced; candidate identity reverified; backup/journal storage verified root-owned and restrictive | Exact source bytes copied from verified descriptor with pre/post identity checks; restoration metadata and digests recorded; backup file and directory fsynced; generation 1 publishes identities, sanitized baseline facts, and `BACKUP_DURABLE` | Interrupted initial publication deletes non-authoritative staging and infers no activation recovery; source/service remain untouched |
| `BACKUP_DURABLE -> CONFIG_COMMITTING` | Candidate/source/adopted-path artifacts and earlier baseline facts authenticate; commit primitive available | Publish successor generation recording ambiguous intent to modify the active name | No exchange may begin until publication is durable; later recovery classifies the actual active namespace from published intent |
| `CONFIG_COMMITTING -> PRE_EXCHANGE_REVALIDATED` | After durable intent, freshly recheck exact original source at active name, candidate identity/content/metadata, adopted authority, parent identity, and the complete healthy service baseline against journaled facts | Read-only, in-memory state only; perform no journal write or unrelated work before exchange | If any fact differs while active is exact original, publish and reverify `PRECOMMIT_ABORT`; if active is not exact original, classify candidate versus unknown before proceeding |
| `PRE_EXCHANGE_REVALIDATED -> CONFIG_COMMITTED` | Immediately invoke the race-aware same-directory exchange; displaced object must be exact expected source and new active object exact candidate | Atomic namespace exchange, then active file fsync as applicable and parent directory fsync; publish `CONFIG_COMMITTED` before any service action | An interrupted phase publication leaves published `CONFIG_COMMITTING`, which safely classifies the namespace; once exchange may have occurred, publish and reverify failure/rollback intent before any compensating namespace mutation, or fail closed if that cannot be done |
| `{BACKUP_DURABLE, CONFIG_COMMITTING} -> PRECOMMIT_ABORT` | Recovery cannot safely continue or final revalidation failed, and descriptor/digest/metadata checks prove the active name is still the exact original source | Publish precommit-abort decision and authenticated cleanup allowlist before deletion | No cleanup deletion before durable publication; no active-config or service action; unknown/candidate active identity cannot take this path |
| `PRECOMMIT_ABORT -> DURABLY_CLEAN_JOURNAL_NAMESPACE -> FAILED_PRECOMMIT` | Durable abort decision authentic; each existing transaction artifact matches its journaled identity | Remove only authenticated candidate/backup/journaled artifacts idempotently, tolerate already-absent allowlisted artifacts, fsync affected directories, securely unlink the journal, fsync its directory, recheck the namespace clean, then expose the logical failure result | Resume abort cleanup/retirement after crash; no service action and no config rollback; do not finalize failure while a journal may remain or absence is not durable |
| `CONFIG_COMMITTED -> SERVICE_ACTIVATING` | Active bytes/digest, metadata, and adopted name reverified; post-exchange observation confirms the pre-activation service baseline did not disappear/change across the unavoidable race; service phase implemented | Publish `SERVICE_ACTIVATING`, then begin the fixed allowlisted restart or verified reload | Publication failure issues no service command; a baseline mismatch after exchange publishes activation failure/rollback intent before rollback action |
| `SERVICE_ACTIVATING -> SERVICE_VERIFIED` | systemd command succeeded and bounded readiness checks prove expected service/process/config stability | Read-only service observations, then publish verification evidence without secrets | Publish activation failure/rollback intent before any rollback side effect |
| `SERVICE_VERIFIED -> COMMIT_CLEANUP_PENDING` | Active candidate and service stability reverified; success is now irrevocably selected | Publish commit decision and complete cleanup allowlist before any rollback artifact is removed | Publication failure leaves artifacts intact and no user-visible success; recovery still follows the published predecessor phase |
| `COMMIT_CLEANUP_PENDING -> DURABLY_CLEAN_JOURNAL_NAMESPACE -> COMMITTED_SUCCESS` | Durable commit decision authentic; each existing cleanup artifact matches its journaled identity | Remove authenticated artifacts idempotently, tolerate already-absent allowlisted artifacts, fsync every affected directory, securely unlink the journal, fsync its directory, recheck the namespace clean, then expose the logical success result | Resume cleanup/retirement on restart; never roll back solely because a cleanup artifact is absent; do not report success while a journal may remain or absence is not durable |
| `ACTIVATION_FAILED -> ROLLBACK_CONFIG` | Published failure state plus durable backup and/or retained displaced original authenticate | Publish `ROLLBACK_CONFIG`, then restore exact old bytes/metadata using secure same-directory staging and atomic namespace operation; fsync file and directory | Publication failure performs no restoration; later uncertainty becomes `ROLLBACK_FAILED` |
| `ROLLBACK_CONFIG -> ROLLBACK_SERVICE` | Old config digest and metadata verified at adopted name | Publish `ROLLBACK_SERVICE`, then use fixed service activation for restored config | Publication failure performs no service action; otherwise distinguish config-restored/service-unrecovered outcome |
| `ROLLBACK_SERVICE -> ROLLBACK_VERIFIED` | Old file is exact; service is again loaded, active, ready, and stably equivalent to the journaled healthy baseline, with the expected executable/config relationship | Read-only verification, then publish `ROLLBACK_VERIFIED` evidence | Failure becomes a distinct config-restored/service-recovery or rollback failure |
| `ROLLBACK_VERIFIED -> ROLLBACK_CLEANUP_PENDING` | Exact old config and a healthy baseline-equivalent service are proven; rollback is irrevocably selected | Publish rollback-complete decision and cleanup allowlist before artifact deletion | Publication failure leaves artifacts intact and rollback outcome unfinalized |
| `ROLLBACK_CLEANUP_PENDING -> DURABLY_CLEAN_JOURNAL_NAMESPACE -> FAILED_ROLLED_BACK` | Durable rollback decision authentic; each existing cleanup artifact matches its journaled identity | Remove authenticated artifacts idempotently, tolerate already-absent allowlisted artifacts, fsync every affected directory, securely unlink the journal, fsync its directory, recheck the namespace clean, then expose the logical verified-rollback result | Resume cleanup/retirement on restart; missing allowlisted artifacts alone are not indeterminate; return activation failure only after the namespace is durably clean |

Implementation PR A, if intentionally limited to filesystem mechanics, stops
with a test-only/internal transaction boundary and does not advertise a
production-successful activation. Production success requires the service
phase unless review establishes that cloudflared already consumes the new file
without a restart/reload, which must not be assumed.

## Source revalidation and stale-write protection

### Facts compared immediately before commit

The privileged process MUST reread the root-owned adopted setting and require
the same canonical path used for preparation. It then performs a root-to-leaf,
no-follow directory walk or an equivalent Linux `openat2` constrained lookup,
retaining the verified parent descriptor. Every path component must remain a
directory with the expected identity; the leaf must remain a regular file.

The current source is compared with the candidate's PR10 snapshot:

- full SHA-256 of bounded bytes and exact size (authoritative for contents);
- file device and inode;
- UID, GID, and permission mode;
- parent device, inode, ownership, and mode;
- canonical adopted path and leaf name; and
- `mtime_ns` and `ctime_ns` as supplementary evidence.

Timestamps alone are never sufficient. Same bytes in a replacement inode are
still a stale identity and are rejected. The same inode with changed-and-
restored timestamps is rejected by content hashing when bytes differ, and
metadata changes are independently rejected. Unsupported file types, multiple
hard links, unsafe writable ancestry, symlinks, mounts changing underneath the
walk, or an unreadable component fail closed.

PR10 does not currently retain every parent ownership fact listed above.
Implementation PR A must extend the immutable snapshot deliberately (without
putting paths or contents in `repr`) rather than treating an absent fact as a
match.

Hashing is performed from retained descriptors with pre/post `fstat` checks,
bounded reads, and size/digest verification. Path lookup is checked against the
descriptor immediately before commit. Candidate identity is likewise checked
before and after each external validation and before namespace mutation.

### Closing the final pathname race

An ordinary sequence of "check destination; `os.replace(candidate, active)`"
has a race: a manual atomic replacement can occur after the check and be
silently overwritten. An advisory manager lock does not prevent root tools
from doing that.

The proposed Linux commit primitive is a same-directory
`renameat2(RENAME_EXCHANGE)` through the retained parent descriptor. It
atomically puts the candidate at the active name while retaining the displaced
object at the candidate name. The helper must then prove that the displaced
object is the exact snapshotted source. A mismatch is stale interference after
the active namespace may already have changed. The helper MUST NOT immediately
exchange back or restore anything while the published journal still says
`CONFIG_COMMITTING`. It first publishes and reverifies `ACTIVATION_FAILED` and
then `ROLLBACK_CONFIG` through the fixed journal protocol; only that durable
rollback intent can authorize a compensating namespace mutation. If it cannot
publish the intent or cannot prove a compensation preserves the unexpected
operator object, it stops without further namespace mutation and requires
manual recovery. The displaced original, when verified, provides the most
exact immediate rollback object; the separate durable backup covers crash
recovery. A crash before rollback intent remains a `CONFIG_COMMITTING` recovery,
not evidence that compensation began.

For that failure transition, phase-aware journal verification must recognize
the verified candidate at the active name while preserving, but not
authenticating as the source, an unexpected object at the former candidate
name. It must not falsely require the candidate to remain at its pre-exchange
leaf or treat the unexpected object as a trusted rollback artifact. If the
observed namespace cannot be bound safely to this transaction, publication
and automatic compensation fail closed.

This is compare-and-verify, not a true kernel compare-and-swap on destination
inode. A cooperating manager lock plus exchange minimizes the gap, but an
independent root process can still race any userspace protocol. Implementation
PR A MUST prototype the exchange and adversarially test rename, replacement,
open-file writes, and compensation after durable rollback intent. The exact
safe compensation algorithm for an unexpected displaced file is an
implementation review gate, not proven by this design. If it cannot prove
that the operator's state is preserved across crashes and races, production
activation remains disabled. Falling back to unchecked `os.replace` is not
allowed.

The transaction never automatically reparses a changed source and reapplies
the requested operation. The caller must start over from a fresh read so an
operator can review the new baseline.

## Filesystem commit design

Candidate and active names MUST reside in the same verified directory and
filesystem. Candidate creation remains exclusive, unpredictable, no-follow,
`0600`, completely written, flushed, and fsynced. All operations use a retained
directory descriptor and validated leaf names rather than reconstructing
authority from absolute paths.

Before commit, the privileged layer applies the source's intended UID, GID, and
mode to the candidate using descriptor-based operations. Ownership is set
before final mode because ownership changes may clear special bits. The result
is reread and compared. The implementation must define whether special mode
bits are supported; the safe initial policy is to reject them rather than copy
unexpected privilege semantics.

PR10's `CandidateFile` deliberately requires `0600` while it is a disposable
validation artifact. Therefore metadata conversion occurs only after all PR10
validation and a final `CandidateFile.require_intact()` check. The activation
layer must explicitly consume that object into a commit-stage identity, retain
the same inode and content digest, apply metadata through its descriptor, fsync
the complete file, fsync the verified candidate/active parent directory to
make the candidate entry durable, and then reverify candidate identity and
metadata through retained descriptors. All of this precedes any durable
journal generation that names the candidate as a required artifact. It must
not weaken PR10's pre-validation `0600` rule or accidentally call the old
verifier after changing the mode.

The ordering for a successful filesystem commit is:

1. complete candidate contents and metadata conversion, fsync the candidate
   file, fsync its verified active/candidate parent directory, and reverify its
   identity and metadata;
2. create and fsync the exact backup and metadata;
3. fsync the backup directory;
4. only now publish and reverify initial generation `BACKUP_DURABLE`,
   identifying source, candidate, backup, and service baseline through the
   fixed-leaf protocol;
5. publish and reverify successor `CONFIG_COMMITTING` through that protocol;
6. freshly revalidate the exact source and candidate identities, adopted
   authority, parent identity, metadata, and complete healthy service baseline;
7. with no intervening journal publication or unrelated work, perform the
   race-aware same-directory namespace exchange;
8. verify both the new active file and displaced source identities; if the
   displaced object is unexpected, do not exchange back before durable
   failure and rollback intent;
9. fsync the new active file if the platform/filesystem requires reopening it;
10. fsync the active parent directory; and
11. publish and reverify `CONFIG_COMMITTED` before any service activation.

If step 1 fails, no journal may claim the candidate as a durable required
artifact. If step 6 fails and the active name is still provably the original
source, the transaction publishes and reverifies `PRECOMMIT_ABORT` before
cleaning only authenticated transaction artifacts. If exchange may have
occurred in step 7, any subsequent failure first publishes and reverifies
`ACTIVATION_FAILED` and `ROLLBACK_CONFIG` before a compensating namespace
mutation. Failure to publish or verify that intent leaves the namespace
untouched by compensation and requires recovery; a later process can
distinguish published commit intent from published rollback intent.

Failure of any required fsync is failure, not a warning. File fsync does not
make the directory-entry change durable; directory fsync is required after
rename, exchange, unlink, and journal/backup lifecycle changes.

### ACLs, extended attributes, and other metadata

Replacing an inode can lose ACLs, extended attributes, capabilities, security
labels, file flags, or other filesystem-specific metadata even when
UID/GID/mode are copied. The first implementation MUST enumerate supported
metadata before commit. The minimum safe initial policy is:

- require one regular link and ordinary UID/GID/mode;
- reject POSIX ACLs beyond the mode-equivalent base ACL;
- reject extended attributes, capabilities, immutable/append-only flags, or
  security labels unless the implementation explicitly copies and verifies
  them with tests on the supported deployment platform; and
- provide a sanitized administrator-facing reason that metadata is unsupported.

Silently dropping metadata is forbidden. If common target deployments require
SELinux or another label, preservation plus post-rename verification must be a
separately reviewed capability before those files are accepted.

A simple `copy backup; os.replace(candidate, config)` is not a complete
transaction: it does not bind the destination identity, prove exact backup
durability, preserve all required metadata, fsync namespace changes, record
crash state, activate the service, verify readiness, or recover safely.

## Backup design

The minimum safe first design uses one ephemeral rollback backup per active
transaction, not an unbounded history. It lives in a fixed root-owned manager
state directory, separate from candidate names, with a fixed schema and an
internally generated transaction identifier. Browser input cannot select its
path or filename.

The backup contains the exact bytes read from the verified precommit source
descriptor, not a YAML render. It is written with exclusive no-follow creation,
restrictive `0600` permissions, root ownership, bounded complete writes, file
fsync, and backup-directory fsync. A separate authenticated metadata record
contains schema version, transaction ID, adopted-path fingerprint rather than
browser-visible path, source/candidate/backup digests and sizes, device/inode
facts, UID/GID/mode, supported metadata inventory, and journal phase. It must
not contain raw YAML, tokens, validator output, or credentials.

Before commit, the helper verifies that backup bytes hash to the snapshotted
source and that recorded restoration metadata matches that source. Backup and
journal permissions acknowledge that cloudflared configs may contain secrets.
The web identity cannot read them.

On verified success, the helper first publishes and reverifies
`COMMIT_CLEANUP_PENDING`, including the exact allowlist and identities of
artifacts to remove. Only then may the ephemeral backup and other rollback
artifacts be removed. Each deletion is idempotent: an existing object must
match its journaled identity before deletion, while an already-absent
allowlisted object is expected after a cleanup crash. Every affected directory
is fsynced before the journal is unlinked. The journal directory is then
fsynced and rechecked clean before logical result `COMMITTED_SUCCESS` is
reported. No persistent `COMMITTED_SUCCESS` journal is written.

After verified rollback, the same ordering applies through
`ROLLBACK_CLEANUP_PENDING` before artifacts are removed, their directories are
fsynced, and the journal is durably retired. Only then is logical result
`FAILED_ROLLED_BACK` exposed. Before either cleanup-pending decision, a missing
required backup remains a rollback/recovery failure. On rollback failure or
ambiguous pre-decision recovery, artifacts are retained for root administrator
recovery. Automatic age-based deletion MUST NOT remove an artifact referenced
by any transaction journal.

For a proven pre-exchange abort, `PRECOMMIT_ABORT` is published and reverified
before the backup or candidate is deleted. Cleanup then follows the same
authenticated, idempotent deletion, directory-fsync, journal-retirement, and
clean-namespace rules and ends by exposing logical result `FAILED_PRECOMMIT`.
Because the exact original source remains active, this path never restores
config bytes and never invokes service control.

Bounded historical backups are not required for the first implementation.
They increase secret retention and require a separate retention/audit policy.
Operators who need historical configuration should use an independently
secured configuration-management system.

## Service activation and readiness model

### Required healthy baseline

The first production activation implementation supports only a service that is
already healthy. The helper verifies a `SERVICE_BASELINE_VERIFIED` baseline
before `PRECOMMIT_REVALIDATED`, persists sanitized baseline facts before commit
intent, and freshly rechecks the complete baseline after durable
`CONFIG_COMMITTING` and immediately before namespace exchange. At minimum the
baseline requires:

- the fixed `cloudflared.service` unit is loaded;
- `ActiveState` is `active` and `SubState` is the expected running state for the
  supported unit type;
- `MainPID` is positive and stable across a bounded observation interval;
- process start identity is unchanged, preventing PID-reuse confusion;
- `/proc/<MainPID>/exe` or an equivalent check identifies the expected
  cloudflared executable;
- the effective service configuration refers to the explicitly adopted config,
  using strict parsing and identity comparison rather than substring matching;
- the process and unit remain ready/healthy for the bounded baseline stability
  interval; and
- observations before and after readiness agree on unit, PID, process start,
  executable, and adopted-config relationship.

An inactive, failed, restart-looping, transitional, wrong-executable,
wrong-config, unstable, or unverifiable service found by either pre-exchange
check fails closed before active-file namespace mutation. The helper does not
start, restart, reload, or repair it as part of the first activation design.
The administrator must establish a healthy baseline independently and begin a
new transaction.

The `BACKUP_DURABLE` journal record, which is durable before
`CONFIG_COMMITTING`, captures only sanitized baseline facts: an allowlisted unit
identity, normalized load/active/substate enums, baseline MainPID and
process-start identity, executable device/inode and/or trusted digest/version
identity, an adopted-config fingerprint and source digest relationship, the
bounded stability duration/result, and any bounded restart-counter/timestamp
facts required to detect instability. It contains no raw `ExecStart`, config or
executable path, command line, environment, token, YAML, stdout, or stderr.

Immediately after a successful exchange and before issuing the planned service
operation, the helper compares live PID/start identity, unit state, executable,
config relationship, restart counters, and relevant timestamps with the
journaled baseline. A changed or ambiguous continuity record means the service
changed after the final pre-exchange check; that is activation failure and
enters rollback without attempting to reinterpret the new state as healthy.
Post-operation verification must likewise distinguish the one planned
lifecycle operation from any unplanned exit or restart. If that distinction
cannot be proven from bounded systemd/process facts, it fails closed and rolls
back. These observations detect the race; they do not make process liveness and
filesystem exchange atomic.

Rollback does not promise to recreate the same PID. "Previous service state
restored" means that the exact old config is active and a newly observed
service is healthy and equivalent to the journaled baseline in unit,
executable, adopted-config relationship, readiness, and bounded stability. The
old MainPID/process-start facts prove what was healthy before mutation and
prevent the transaction from inventing a baseline after failure.

Candidate acceptance has distinct layers:

1. round-trip document and structural mutation validation;
2. existing application parser validation;
3. `cloudflared tunnel --config <FD-bound-candidate> ingress validate`;
4. healthy precommit service baseline verification;
5. durable filesystem commit verification;
6. systemd operation result;
7. systemd active state and stable positive `MainPID`;
8. process/config identity and meaningful post-start readiness; and
9. a bounded stability window with unchanged process identity.

`systemctl restart` returning zero proves only that systemd accepted/completed
that job. It is not activation success. The service could exit immediately,
restart-loop, run a different config, or be unable to reach Cloudflare.

The implementation PR must inspect the installed cloudflared version and its
unit before choosing reload or restart. Reload is preferred only if the
specific cloudflared/systemd combination has documented, testable semantics
that reread the intended config and expose failure. No safe reload capability
is assumed by this design. Otherwise use restart.

Systemd commands use an absolute verified `systemctl`, a fixed unit name
`cloudflared.service`, fixed argv, `shell=False`, a minimal environment,
bounded output, and bounded timeouts. The helper never accepts a unit name or
systemctl verb from the caller.

At minimum, post-operation verification obtains `LoadState`, `ActiveState`,
`SubState`, `MainPID`, restart counters/timestamps useful for stability, and
the effective `ExecStart` config relationship without exposing raw command
lines. It checks state before and after readiness and requires a stable,
positive MainPID. A process identity check should bind `/proc/<MainPID>` facts
to the expected cloudflared executable and config. The exact application-level
readiness signal is unresolved: candidates include a syntactically valid
ingress table, but meaningful tunnel connectivity may require cloudflared
metrics, logs, or another local signal. Implementation PR B must define and
test that signal and the stability window before production activation is
enabled.

## Rollback contract

Failure after durable `CONFIG_COMMITTING` intent does not by itself require
rollback. If descriptor, digest, and metadata checks prove the active name is
still the exact original source, the transaction enters `PRECOMMIT_ABORT`,
publishes and reverifies its cleanup allowlist, removes only authenticated
transaction artifacts, fsyncs their directories, retires and directory-fsyncs
the journal, rechecks the journal namespace clean, and only then returns
`FAILED_PRECOMMIT`. It performs no active-config write and no cloudflared start,
stop, restart, or reload.

Any failure after the active namespace changed or may have changed enters
rollback. In particular, an unexpected displaced object after
`RENAME_EXCHANGE` never triggers an immediate exchange-back under published
`CONFIG_COMMITTING`. The helper must durably publish and reverify
`ACTIVATION_FAILED` and `ROLLBACK_CONFIG` before any compensating namespace
operation. If publication or safe compensation cannot be proven, it stops
without further namespace mutation and retains recovery artifacts. The
rollback procedure:

1. authenticates the journal, healthy pre-activation baseline, backup, adopted
   path, and current active state;
2. restores the exact prior bytes, preferably by reversing the retained
   exchange when identity is still proven, otherwise by securely staging the
   authenticated backup in the active directory;
3. restores and verifies intended UID, GID, mode, and every supported metadata
   item;
4. fsyncs the restored file and active directory in the required order;
5. uses the fixed service operation to return cloudflared to the prior config;
6. verifies service/process/readiness state is stably equivalent to the
   journaled healthy baseline; and
7. verifies the restored active digest and metadata before completing the
   failure result.

Rollback MUST NOT overwrite an unknown third-party state merely because a
backup exists. If the current active entry matches neither the journaled
candidate nor a known transaction artifact, automatic restoration stops and
reports `ROLLBACK_FAILED`/manual intervention. This avoids compounding a
concurrent administrator change.

The result model distinguishes at least:

- `SERVICE_BASELINE_UNAVAILABLE`: no supported healthy baseline existed, so the
  transaction stopped before active namespace mutation and no rollback was
  attempted;
- `FAILED_PRECOMMIT`: durable transaction recovery state existed, but
  pre-exchange validation or recovery aborted while the active name was proven
  to remain the exact original source; authenticated cleanup completed
  durably, the journal namespace was proven clean, and no service action
  occurred;
- `ACTIVATION_FAILED_ROLLBACK_VERIFIED`: activation failed; exact prior config
  and a healthy service state equivalent to the captured baseline were proven
  restored, rollback cleanup is durable, and the journal namespace was proven
  durably clean;
- `CONFIG_RESTORED_SERVICE_RECOVERY_FAILED`: exact config restoration was
  proven but cloudflared could not be returned to verified health;
- `ROLLBACK_PARTIAL_FAILURE`: some restoration step failed and current state is
  known but not fully restored;
- `ROLLBACK_FAILED_STATE_INDETERMINATE`: current file/service state cannot be
  safely classified; automatic mutation stops;
- `TERMINAL_CLEANUP_REQUIRED`: commit or verified rollback has a durable
  decision, but authenticated cleanup, journal retirement, or namespace
  durability is not complete; it is neither user-visible success nor a new
  rollback decision;
- `RECOVERY_REQUIRED`: a valid recovery-required or unverifiable journal was
  found before a new activation or root manager mutation; the barrier rejected
  the operation without changing release, adopted authority, journal, config,
  or service; and
- `JOURNAL_NAMESPACE_NOT_DURABLY_CLEAN`: secure journal retirement, journal-
  directory fsync, or post-fsync absence verification did not complete. No
  final transaction result or authority mutation may be reported, and config
  and service remain untouched by an authority-gate failure.

All are failures. None may produce an HTTP or CLI success result for the
requested mutation. Details exposed outside the root boundary remain
sanitized.

## Crash and power-loss analysis

### Decision: require a minimal durable journal

Without a journal, states before active replacement are easy to clean by
verified candidate naming, but states immediately after replacement are
ambiguous: a later process cannot know which backup belongs to the active
candidate, whether service activation began, or whether a candidate-looking
file is safe to delete. Refusing all automatic recovery avoids damage but can
leave cloudflared running from a config that no longer matches disk and gives
the administrator too little authenticated information to recover.

The first production implementation that can change the active name therefore
requires a minimal durable single-transaction journal. This is the smallest
safe choice; it is not a general event log. The journal contains a schema
version, transaction ID, generation, predecessor generation/digest where
applicable, phase, release ID, non-secret adopted-path fingerprint,
source/candidate/backup digests and sizes, required identity/metadata facts,
sanitized `SERVICE_BASELINE_VERIFIED` facts, and artifact names chosen by the
helper. Cleanup-pending records additionally contain the complete allowlist and
expected identities of artifacts eligible for deletion. The journal is
root-owned `0600`, bounded, strictly parsed, and published through the one
fixed-leaf protocol below at each recovery-relevant transition.

The journal is not trusted merely because it is root-owned. Recovery verifies
every referenced artifact through fixed directories, strict leaf-name grammar,
no-follow descriptors, digest/size/metadata checks, and current adopted/release
identity. Unknown schema, duplicate fields, malformed content, impossible
transition, unsafe permissions, or identity mismatch cause fail-closed manual
recovery. No artifact path from the journal may escape its fixed directory.

### Fixed-leaf journal publication protocol

The journal directory recognizes exactly two leaf names:

- `journal` is the only authoritative published recovery record; and
- `journal.next` is the only permitted non-authoritative publication staging
  object.

Every journal generation is a complete self-contained record, not a delta. It
contains a monotonically increasing generation number. Initial publication is
generation 1 with no predecessor. Every successor has the same transaction ID,
generation exactly equal to the published generation plus one, and the
published predecessor generation and full-record digest. Generation helps
validate a live successor; it never makes a leaf authoritative. Only the fixed
published `journal` name can carry authority.

Initial publication and every phase advance use exactly this sequence:

1. hold the shared outer lock and any required activation lock;
2. for a successor, authenticate the published `journal`, its generation,
   complete contents, phase-appropriate artifact locations and identities,
   authority, and current phase; for generation 1, first establish a durably
   clean journal namespace;
3. require `journal.next` to be absent; a pre-existing staging leaf is handled
   only by the explicit recovery rules below;
4. exclusively create `journal.next` through the verified journal-directory
   descriptor without following symlinks, and require a regular, single-link,
   root-owned `0600` file;
5. write the complete successor record with bounded complete writes;
6. fsync `journal.next`;
7. strictly parse and verify the staged bytes from the retained descriptor;
8. require the expected schema and phase-appropriate authority/artifact
   identities and, for a successor, the same transaction ID, generation
   `current + 1`, matching predecessor generation/digest, and an allowlisted
   phase transition;
9. atomically rename `journal.next` over `journal` through the verified
   directory descriptor;
10. fsync the journal directory;
11. reopen `journal` without following symlinks and reverify its type,
    ownership, mode, link count, exact bytes/digest, transaction ID,
    generation, phase, and directory/path binding; rescan through the same
    descriptor and require `journal.next` and every unknown journal-like leaf
    to be absent;
12. only then treat the successor phase as durably authoritative; and
13. only then begin any filesystem, artifact-cleanup, or service side effect
    whose permission depends on that successor phase.

Rename success alone is not publication success. If step 10 or 11 fails, the
process stops without beginning the successor-dependent side effect. A phase
that records an already completed side effect may be published afterward only
when its predecessor phase was itself sufficient to authorize and recover that
effect. For example, durable `CONFIG_COMMITTING` authorizes the config exchange;
if a later `CONFIG_COMMITTED` publication is interrupted, recovery from the old
`CONFIG_COMMITTING` phase can still classify the active namespace safely.

The initial generation obeys a stronger rule: before generation 1 has completed
steps 9 through 12, no active-config namespace mutation, cloudflared service
action, rollback action, or transaction-artifact deletion is permitted.
Candidate and backup staging may exist, but they are not active side effects
and are recoverable only under their separately verified orphan policy. This
invariant makes a lone unpublished `journal.next` safe to discard without
inferring activation recovery from its contents.

### Interrupted publication recovery

Recovery first verifies the fixed journal directory and performs a bounded,
no-follow namespace scan under the shared outer lock. The only accepted shapes
are:

1. **`journal` exists; `journal.next` is absent.** Authenticate `journal`, fsync
   the journal directory, reopen/reverify the published record and path binding,
   then recover from its phase.
2. **Both leaves exist.** This is the expected pre-rename crash shape, not
   automatically ambiguity. `journal` remains the sole authority. Authenticate
   it first. Then require `journal.next` to have exactly the fixed staging name,
   regular-file type, root owner, `0600` mode, one link, and verified directory
   identity. Do not trust or promote its contents. Securely unlink it, fsync the
   journal directory, reopen/reverify `journal`, and continue from the published
   phase. If `journal` is invalid, fail closed without using a valid-looking
   staging record to rescue it.
3. **`journal` is absent; `journal.next` exists.** Treat it only as interrupted
   initial publication. Verify the staging object's exact filesystem identity,
   but do not infer recovery state from or promote its contents. Securely unlink
   it, fsync the journal directory, and establish the durably clean namespace.
   This is safe only because the initial-publication invariant forbids active-
   config, service, rollback, or cleanup side effects before generation 1 is
   durably published; an implementation unable to maintain that invariant must
   not use this protocol. Candidate or backup orphans are handled only by their
   separate fixed-name/identity policy, never by trusting staged journal
   contents.
4. **Neither leaf exists.** Fsync the verified journal directory, rescan it, and
   require the namespace to remain clean before proceeding.
5. **Any other shape.** An extra staging object, alternate journal name,
   symlink, wrong type/owner/mode/link count, unsafe directory identity, or
   other journal-like entry fails closed without deletion or promotion.

A recognized `journal.next` is removed because it is an unpublished staging
artifact, not because its serialized phase appears older or newer. A partial or
malformed staging write is therefore handled identically to a complete staged
successor when the authoritative `journal` is valid. Recovery never renames
`journal.next` to `journal`.

Power loss before publication rename leaves the old `journal` authoritative
and may leave `journal.next` for rule 2 cleanup. Power loss after rename but
before journal-directory fsync may expose either the old published generation
(possibly with the staging leaf) or the new published generation after reboot.
Recovery applies the namespace rules above, fsyncs the visible supported shape,
and continues from whichever authentic `journal` is visible. Both outcomes are
safe because no side effect dependent on the successor phase began before
publication durability. Once directory fsync and published-record
reverification succeed, the successor generation is durable and its guarded
side effect may begin.

### One journal lifecycle

The governing invariant is: **the activation journal exists only while
transaction or recovery state may still require durable recovery**. It is not
an audit log. `COMMITTED_SUCCESS`, `FAILED_PRECOMMIT`, and
`FAILED_ROLLED_BACK` are final result names only and MUST NOT be intentionally
left on disk as journal phases.

Artifact presence is interpreted by the durable decision phase. Before a
cleanup decision, every artifact required for rollback must exist and
authenticate; absence is a rollback/recovery failure. In `PRECOMMIT_ABORT`,
`COMMIT_CLEANUP_PENDING`, or `ROLLBACK_CLEANUP_PENDING`, the journal proves that
the corresponding outcome has already been selected and records the complete
cleanup allowlist. Existing allowlisted artifacts must still authenticate
before deletion, but missing allowlisted artifacts mean cleanup may already
have progressed and are not by themselves indeterminate.

After any of those decisions, the only permitted finalization sequence is:

1. authenticate the published `journal`, its current release/adopted authority,
   and every remaining allowlisted artifact, and require `journal.next` absent
   after applying interrupted-publication recovery if necessary;
2. remove allowlisted artifacts idempotently and reject any existing object
   with an unexpected identity;
3. fsync every directory affected by artifact cleanup;
4. securely unlink authoritative `journal` through the verified journal-
   directory descriptor; do not create `journal.next` merely to retire it;
5. fsync the journal directory;
6. rescan through that same descriptor and prove that neither recognized leaf
   nor any unknown journal-like object exists; and
7. only then expose the corresponding logical final result.

If any step fails, the final result is not exposed. Recovery repeats the same
decision-specific cleanup and retirement. A crash after journal unlink but
before its directory fsync is safe: authority is still unchanged. On the next
run the journal may be visible or absent. If visible, it is authenticated and
cleanup/retirement resumes; if absent, absence still must be made durable by
the directory-fsync-and-recheck procedure below. If that crash lost the
in-memory final result, even a subsequently proven clean namespace does not
authorize reconstructing one; any historical result must come from a separate
audit channel. Only successful directory fsync and clean rescan establish that
no activation recovery remains.

`CONFIG_COMMITTING` is intentionally ambiguous. Recovery MUST first classify
the active name using retained/journaled identity, full digest, size, and
metadata facts:

- exact original source means exchange did not take effect, so publish and
  reverify `PRECOMMIT_ABORT` and perform authenticated cleanup with no service
  action;
- exact candidate means exchange took effect; verify the displaced object and
  continue committed-state recovery only if it is the exact source. If it is
  unexpected, publish and reverify failure/rollback intent before any safe
  compensation, or fail closed; and
- anything else is indeterminate concurrent interference and requires manual
  recovery without deleting artifacts or controlling the service.

The same classification governs a crash in the in-memory
`PRE_EXCHANGE_REVALIDATED` state because its durable journal still says
`CONFIG_COMMITTING`.

### Durably clean journal namespace and recovery barrier

Descriptor-held locks prevent concurrent work only while their owning process
is alive. The fixed journal namespace therefore remains an authority barrier
after a crash. `DURABLY_CLEAN_JOURNAL_NAMESPACE` is a proven condition, not a
journal state or a remembered pathname lookup. It is established only while
holding the shared outer lock and a verified descriptor for the fixed,
root-owned, restrictive journal directory.

The implementation performs a bounded no-follow scan of that directory and
classifies it as follows. The fixed path-to-directory-descriptor binding,
parent identity, directory type, ownership, and restrictive mode are verified
before and after the scan/fsync sequence; a rename, replacement, or identity
change fails closed.

- valid authoritative `journal` with no staging leaf is authenticated and
  classified. Remaining transaction, cleanup, or recovery work blocks authority
  mutation and directs the root administrator to recovery;
- authoritative `journal` plus recognized `journal.next` first follows rule 2
  of interrupted-publication recovery: authenticate `journal`, remove only the
  filesystem-verified staging leaf, fsync the directory, and continue solely
  from `journal`. It is not automatically an ambiguous namespace;
- staging-only `journal.next` follows rule 3: because initial publication could
  not have authorized a mutating side effect, remove the filesystem-verified
  staging leaf, fsync the directory, and continue to the clean-namespace proof
  without parsing, promoting, or inferring recovery from it;
- a cleanup-decision `journal` whose allowlisted artifacts are absent may be
  authenticated against the **current** release/adopted authority; artifact
  absence is reverified, every affected artifact directory is fsynced again,
  and retirement finishes using steps 4 through 6 above;
- neither recognized leaf still requires journal-directory fsync and a rescan
  proving both remain absent; and
- malformed or unverifiable authoritative `journal`, unsafe recognized-leaf
  metadata, unknown/extra journal-like objects, alternate names, or any other
  namespace shape fails closed unchanged. A valid-looking `journal.next` never
  rescues an invalid `journal`.

Only completed cleanup-decision retirement or the verified neither-leaf path
can establish `DURABLY_CLEAN_JOURNAL_NAMESPACE`. Directory fsync or post-fsync
rescan failure closes the barrier. A cleanup-decision journal identity mismatch
is unverifiable; it is never treated as obsolete history, absence, or
permission to delete. A blocked operation returns a sanitized instruction for
activation status/recovery and MUST NOT change `current`, a release, the
adopted setting, journal/artifacts, cloudflared config, or service state.

The conservative first policy is that all root manager mutation transactions
refuse while this barrier is closed, rather than trying to decide whether a
particular mutation happens to preserve activation authority. At minimum the
barrier applies before:

- `cfm-update` or any `current` release switch;
- installer, repair, or reconciliation paths capable of switching/replacing
  the active release or persistent manager authority;
- `cfm-config cloudflared-config adopt-detected`; and
- `cfm-config cloudflared-config clear`.

Read-only status may remain available and should report only a sanitized
"activation recovery required" condition when appropriate. A historical audit
record, if one is later implemented, is separate from the transaction journal,
does not authenticate recovery, and cannot open or close this barrier.

### Crash matrix

| Crash point | Durable state that may remain | Required next-run behavior |
| --- | --- | --- |
| Before candidate creation | Source only; no journal observed | No transaction recovery is indicated, but any privileged transaction or authority mutation still fsyncs the verified journal directory and rechecks absence before proceeding |
| During candidate write, before candidate fsync | Incomplete hidden candidate | Never activate it; remove only after name/inode/type/ownership checks, otherwise fail closed |
| After candidate file fsync or validation, before metadata conversion and parent-directory fsync | Candidate bytes may be durable, but its directory entry and intended commit metadata are not yet proven durable; active unchanged and no journal may require the candidate | Revalidate and explicitly resume only within the same live transaction; after process loss, securely discard a verified orphan or fail closed on identity mismatch |
| After candidate metadata conversion and file fsync, before candidate/active parent-directory fsync | Candidate entry may disappear on power loss; active unchanged and no journal may require it | Do not publish `BACKUP_DURABLE`; fsync the verified parent directory and reverify candidate identity, or abort without active mutation |
| After candidate/active parent-directory fsync and candidate reverification, before initial journal publication | Candidate entry, contents, and intended metadata are durable, but active is unchanged and candidate is not yet journaled authority | A new process handles an orphan only under the separate verified artifact policy; no activation recovery is inferred from candidate presence alone |
| During or after service baseline observation, before durable journal | Active config/service unchanged; candidate may remain; no durable baseline authority | Securely discard candidate after identity checks; a new transaction must establish a fresh bounded healthy baseline |
| Baseline is unhealthy or becomes unstable | Active config namespace unchanged | Fail closed with `SERVICE_BASELINE_UNAVAILABLE`; never start/restart/reload service and never enter commit |
| Any crash leaves a valid recovery journal | Descriptor locks are released, but journaled release/adopted authority or cleanup/retirement work remains | Persistent barrier blocks update/install/reconciliation/adopt/clear under the shared outer lock until recovery retires the journal and proves the namespace durably clean |
| Authoritative `journal` is malformed, unknown, unsafe, or unverifiable after crash | Authority cannot be authenticated safely, regardless of apparently valid `journal.next` contents | Fail closed exactly like pending recovery; do not delete/promote staging or perform manager/config/service mutation; direct root to status/manual recovery |
| Crash halfway through `journal.next` write | Valid old `journal` plus partial recognized staging, or staging only during generation 1 | With valid old authority, verify staging filesystem identity, unlink it without parsing, fsync directory, and recover old phase; staging-only uses the initial-publication invariant, deletes/fsyncs staging, and infers no activation recovery |
| Crash after `journal.next` fsync or immediately before rename | Valid old `journal` plus complete but unpublished staging, or complete generation-1 staging only | Same as partial staging: published `journal` alone is authority; never promote staging based on valid contents or generation |
| Rename `journal.next -> journal` completed, crash before journal-directory fsync | No successor-dependent side effect began; reboot may expose authentic old or new `journal`, with the pre-rename shape also permitted | Apply the fixed namespace classifier, fsync the supported visible shape, reopen/reverify authoritative `journal`, and resume its phase; old and new are both safe because publication had not authorized a dependent side effect before durability |
| Journal-directory fsync succeeded, crash before published-record reverification | The renamed generation is directory-durable, but this process authorized no successor-dependent side effect | Recovery authenticates and reverifies the published `journal` and clean staging namespace before treating that generation as authority for its guarded effect |
| Journal-directory fsync and published-record reverification completed | New generation is durably authoritative; `journal.next` is absent | Only now may the filesystem/service/cleanup side effect guarded by the new phase begin |
| Valid old `journal` plus malformed `journal.next` | Old journal is authoritative; staging may be an interrupted short write | Verify only staging filesystem identity, unlink it, fsync directory, reopen old journal, and recover from old phase |
| Invalid old `journal` plus valid-looking `journal.next` | No trustworthy authority | Fail closed; staging never rescues, replaces, or supplies authority for invalid published state |
| `journal.next` exists without `journal` | Unpublished initial generation only; initial-publication invariant proves active config/service/rollback/cleanup were untouched | Verify staging filesystem identity, unlink it without using contents, fsync and rescan the journal directory, and infer no activation recovery |
| Staging symlink, wrong metadata/link count, alternate or extra journal-like object | Namespace is outside the fixed two-leaf protocol | Fail closed without deletion, promotion, authority mutation, config mutation, or service action |
| Cleanup-decision journal remains after all allowlisted artifacts are absent | Logical outcome is selected, but journal retirement may still require durability | Authenticate against current authority, reverify and fsync affected artifact directories, then securely unlink/fsync/recheck the journal namespace; do not expose the final result earlier |
| Immediately before journal unlink | Durable cleanup decision and clean artifact directories; journal still provides recovery authority | Crash leaves the journal authoritative; next recovery revalidates completed cleanup and retries retirement; authority remains blocked |
| Immediately after journal unlink or after reboot observes absence, before journal-directory fsync | Namespace mutation may not yet be durable; authority is unchanged | Fsync the verified journal directory and recheck absence under the outer lock; if the journal reappears, authenticate and resume its recorded cleanup decision; never infer cleanliness from `ENOENT` alone |
| Second crash while making observed absence durable | Authority mutation has not begun; disk may recover journal-present or journal-absent namespace | Repeat the same descriptor-bound fsync and absence recheck; operation is idempotent and remains blocked until it proves durable cleanliness |
| After journal-directory fsync and clean rescan, before authority mutation | Durably clean journal namespace; current release/adopted authority is still unchanged | Safe boundary: a later operation re-establishes the clean condition and revalidates current authority; no activation recovery or cloudflared action is needed |
| After authority mutation begins | Old activation journal unlink and clean namespace are already durable; the update/adopt/clear transaction may have its own incomplete state | Recover only through that operation's own transaction contract; no old activation journal may legitimately reappear and no activation recovery step may require old release/adopted authority |
| During backup write, before backup fsync | Incomplete backup; active unchanged | Journal must not claim `BACKUP_DURABLE`; remove authenticated incomplete artifacts or require manual recovery on mismatch |
| After backup fsync but before backup-directory fsync | Backup existence is not durable | Active unchanged; repeat/clean backup creation; do not commit |
| After durable backup but before initial journal publication begins | Active config/service unchanged; orphan durable backup possible | Discover only through fixed bounded artifact policy; never infer authority from filename alone; clean if identity can be proven |
| After durable `BACKUP_DURABLE` journal, before commit intent | Active unchanged; backup, candidate identities, and sanitized healthy baseline are journaled | Revalidate source/candidate and establish that the live service still matches the recorded baseline; if recovery cannot continue, publish and reverify `PRECOMMIT_ABORT` before cleaning without touching active |
| After durable `CONFIG_COMMITTING`, before fresh recheck | Commit intent is durable but active-name mutation is unknown; prior healthy baseline is durable | Classify active name first: exact original means publish and reverify `PRECOMMIT_ABORT`; exact candidate means recover as committed; anything else is indeterminate/manual recovery |
| After in-memory `PRE_EXCHANGE_REVALIDATED`, before exchange | Journal still says `CONFIG_COMMITTING`; active should be original but crash timing is authoritative only through current identity | Apply the same three-way classification; never infer exchange from the in-memory phase |
| Fresh pre-exchange source/candidate/adopted/baseline check fails with exact original active | Active config has not changed and the transaction has invoked no service action; durable intent and artifacts remain | Publish and reverify `PRECOMMIT_ABORT`; only then perform authenticated abort cleanup; do not exchange or invoke service control |
| During `PRECOMMIT_ABORT` cleanup | Original source remains active; some allowlisted artifacts may already be absent | Authenticate/delete remaining artifacts idempotently, fsync affected directories, retire/fsync/recheck the journal namespace, then expose logical `FAILED_PRECOMMIT`; identity mismatch requires manual recovery |
| Immediately after atomic exchange, before displaced-object verification | Candidate may be active; displaced object may be original or unexpected; directory change may not be durable; published journal still says `CONFIG_COMMITTING` | Use journal plus identities to classify; do not perform an unjournaled exchange-back; publish and reverify failure and rollback intent before any compensating namespace mutation, or fail closed |
| Displaced object is unexpected after exchange, before durable rollback intent | Candidate may be active and an operator object may be at the candidate name; `CONFIG_COMMITTING` remains published | Preserve both objects; publish and reverify `ACTIVATION_FAILED` and `ROLLBACK_CONFIG` before safe compensation, or stop for manual recovery without further namespace mutation |
| Crash after durable `ROLLBACK_CONFIG` intent, before compensation | Published rollback intent distinguishes this from `CONFIG_COMMITTING`; the earlier exchange may or may not have survived power loss | Reclassify the active and displaced names against source, candidate, backup, and any unexpected operator object before acting; compensate only if identity-preserving behavior is proven, otherwise stop for manual recovery |
| Service baseline changes after the final check, during exchange, or after exchange | Candidate may be active while the process no longer matches the baseline | Post-commit continuity verification detects the mismatch; publish and reverify `ACTIVATION_FAILED`, then publish each rollback intent before restoring config and baseline-equivalent service state |
| After active file fsync but before active-directory fsync | File data durable; name swap may not be | Same classification; do not assume either namespace survived power loss |
| After active-directory fsync but before durable `CONFIG_COMMITTED` publication | New active is durable; authoritative journal remains `CONFIG_COMMITTING`; `journal.next` may be partial/complete | Discard recognized staging durably, classify the active namespace from `CONFIG_COMMITTING`, then republish/advance or roll back; issue no service command from staging contents |
| After `CONFIG_COMMITTED` but before service operation | New active durable; old process may still use old config | Resume bounded service activation when PR B supports it; PR A must report recovery required rather than success |
| During restart/reload | New active durable; service state unknown | Reinspect systemd and process identity; verify readiness or roll back; do not trust prior command status |
| After service becomes healthy but before durable `SERVICE_VERIFIED` publication | New active and possibly healthy service; authoritative journal remains `SERVICE_ACTIVATING`; `journal.next` may exist | Discard recognized staging durably, repeat readiness/stability verification from the published phase, and republish; success is not inferred from staging |
| After `SERVICE_VERIFIED` but before durable cleanup-decision publication | Successful active/service state plus all required rollback artifacts; staging may contain a proposed decision | Do not delete artifacts; discard interrupted staging, reverify active/service state from published `SERVICE_VERIFIED`, then publish `COMMIT_CLEANUP_PENDING` or roll back if verification fails |
| After durable `COMMIT_CLEANUP_PENDING`, before or during cleanup | Commit is irrevocably selected; some or all rollback artifacts may remain | Never roll back solely for missing cleanup artifacts; authenticate and remove those still present, accept already-absent allowlisted artifacts, and fsync affected directories |
| After commit cleanup directory fsync, before journal retirement | Commit decision and completed cleanup are durable; journal still says `COMMIT_CLEANUP_PENDING` | Reverify cleanup, securely unlink/fsync/recheck the journal namespace, then expose logical `COMMITTED_SUCCESS`; never write a persistent success journal or report success earlier |
| During rollback staging or namespace restoration | Journal says rollback; active may be candidate or restored source | Classify only known digests and artifacts; continue exact restoration if safe; otherwise `ROLLBACK_FAILED_STATE_INDETERMINATE` |
| After restored file fsync but before directory fsync | Restored bytes exist but namespace durability uncertain | Repeat classification and required fsync; service recovery waits for durable namespace |
| During rollback service recovery | Old config durable; service state unknown | Verify/retry bounded fixed activation and readiness; distinguish config-restored/service-failed |
| After `ROLLBACK_VERIFIED` but before rollback decision | Old config and baseline-equivalent service proven; all required recovery artifacts remain | Do not delete artifacts; publish and reverify `ROLLBACK_CLEANUP_PENDING` first |
| After durable `ROLLBACK_CLEANUP_PENDING`, before or during cleanup | Verified rollback is irrevocably selected; artifacts may be partially absent | Authenticate and remove remaining allowlisted artifacts idempotently, accept already-absent allowlisted artifacts, and fsync affected directories |
| After rollback cleanup directory fsync, before journal retirement | Restored config/service and cleanup are durable; journal still says `ROLLBACK_CLEANUP_PENDING` | Reverify cleanup, securely unlink/fsync/recheck the journal namespace, then expose logical `FAILED_ROLLED_BACK`; never write a persistent rollback-result journal |

Recovery is invoked explicitly inside the privileged boundary before any new
transaction. Merely starting the web service MUST NOT mutate cloudflared or
auto-recover an incomplete transaction. An implementation may provide a
root-only status/recover operation, but it cannot offer arbitrary artifact
selection. Until recovery retires its journal and establishes a durably clean
journal namespace, the persistent barrier prevents manager update/configuration
transactions from changing the release or adopted authority on which recovery
depends.

## Locking and concurrency

Only one cloudflared activation transaction may run at a time. The privileged
component holds a non-blocking exclusive advisory lock in a fixed root-owned
`0700` runtime or persistent state directory for the entire prepare, validate,
commit, service, and rollback sequence. The lock file is fixed, no-follow,
regular, root-owned, `0600`, and descriptor-held.

The current manager deployment/configuration lock protects updates and adopted
setting changes. It becomes the shared outer manager-operation lock for this
contract. A runtime activation lock is not enough: after a crash it is released
while a recovery journal can still depend on the old active release and adopted
path.

Every root manager mutation transaction follows this order:

1. acquire the shared outer manager/deployment lock;
2. open and verify the fixed root-owned journal directory and establish
   `DURABLY_CLEAN_JOURNAL_NAMESPACE`, including directory fsync plus post-fsync
   absence rescan even when the first lookup returned `ENOENT`;
3. revalidate the current manager release and adopted-config authority only
   after the namespace is durably clean;
4. perform the update/configuration operation's own transaction; and
5. release the outer lock only after that operation reaches its own safe
   boundary.

Step 2 applies the complete fixed-leaf recovery classifier. It cleans a
filesystem-verified `journal.next` durably without trusting or promoting it,
then classifies authoritative `journal`; a valid authoritative journal with
remaining recovery or cleanup work blocks. A staging-only leaf is discarded
under the initial-publication invariant before the neither-leaf durability
proof. Malformed authoritative state, unsafe recognized-leaf metadata, unknown
objects, or any otherwise unverifiable shape rejects. If an authenticated
cleanup-decision journal remains but all allowlisted artifacts are absent, step
2 repeats every affected artifact-directory fsync and may then finish
retirement before performing the journal fsync-and-rescan proof. It does not
perform substantive rollback or service recovery as an incidental part of
update/adopt/clear.

This ordering is mandatory for `cfm-update`, installer/reconciliation release
switching, adoption, and clear. The barrier check occurs inside the lock and
immediately before the operation's own authority validation/mutation; checking
before lock acquisition is insufficient, and pathname `ENOENT` is not the
gate. The rejection path does not reconcile units, switch `current`, rewrite
the EnvironmentFile, restart the manager, touch activation state, or control
cloudflared.

Journal retirement and the clean-namespace proof are locked preconditions, not
best-effort housekeeping. If unlink, directory fsync, or absence rescan fails,
the manager mutation aborts without changing the active release, adopted
setting, cloudflared config, or cloudflared service. A process crash after the
journal-directory fsync and clean rescan but before authority mutation is safe:
no recovery state remains and the old authority is still current. A crash
before that proof also cannot expose changed authority because the ordering
forbids authority mutation. The next operation repeats the proof, including
fsync after initially observed absence.

Once an authority mutation is allowed to begin, the old transaction journal
cannot legitimately reappear: its unlink was directory-durable, its namespace
was rescanned clean, and the outer lock excludes another manager transaction.
Disk corruption or an independently created lookalike is not old recovery
state; it is unsafe/ambiguous state and fails closed. Consequently no recovery
step begun after the gate needs the old release or adopted authority.

Activation status/recovery also acquires the same shared outer lock before it
classifies or changes a journal. If a separate activation lock is retained, the
order is always outer manager lock first, activation lock second, and both
remain held through classification plus recovery/cleanup/retirement. A new
activation first establishes the same durably clean namespace; a pending
journal routes to recovery instead. No code may acquire these locks in reverse
order.

Holding the outer lock for the entire activation/recovery transaction is the
conservative first design. It prevents update, install/reconciliation, adopt,
and clear from racing a live recovery. If future review narrows lock duration,
the persistent journal barrier and identical lock/check ordering still must
close every authority-change window. The lock scope and barrier must be tested
together, including crash release of descriptor locks.

Advisory locks coordinate only Cloudflared Manager processes. They do not lock
out root editors, package scripts, cloudflared tooling, or direct systemctl
commands. Immediate stale detection, race-aware exchange, and post-operation
verification remain mandatory.

## Error, audit, and observability model

The privileged layer returns a closed enum/status plus a transaction identifier
safe for correlation. It separates precommit rejection, validation failure,
stale source, commit failure, activation failure with verified rollback,
partial rollback, service recovery failure, and indeterminate recovery.

Exceptions, `repr`, logs, journal fields, HTTP models, and CLI output MUST NOT
include raw YAML, candidate/source/backup paths, credentials, tokens, raw
cloudflared/systemctl output, environment contents, or unvalidated caller
strings. Root-only logs still use allowlisted structured fields and bounded
messages; being root-only is not permission to persist secrets.

Audit facts may include sanitized operation type, transaction ID, release ID,
source/candidate digest prefixes only if collision/confusion risk is addressed,
state transition, result code, timestamps, and whether rollback was verified.
The audit record is not used as transaction authority, does not live in the
transaction-journal namespace, and cannot participate in recovery or block an
authority change. This document does not otherwise design audit persistence.

## PR10 invariants that must survive privilege

The privileged implementation reuses rather than bypasses all PR10 guarantees:

- the shared bounded configuration size;
- strict UTF-8 input;
- canonical/no-follow source checks and regular-file requirements;
- immutable source bytes and identity snapshots;
- round-trip `ruamel.yaml` editing separated from the PyYAML read model;
- duplicate-key, unsafe-tag, malformed, ambiguous-structure, and recursive
  alias rejection;
- only narrow mutation primitives, with terminal catch-all preservation;
- semantic no-op without render or staging;
- exclusive unpredictable same-directory `0600` candidate staging;
- complete candidate writes and candidate fsync;
- retained candidate file and directory identity;
- existing application-parser validation before external acceptance;
- fixed cloudflared ingress validation with bounded timeout;
- FD-bound `/proc/self/fd/<dirfd>/<candidate-name>` lookup;
- explicit `pass_fds`, unrelated descriptors close-on-exec, and `shell=False`;
- sanitized errors and bounded command output; and
- verified candidate cleanup on failure.

Root privilege increases the consequences of a bug. It does not make unsafe
YAML, ambiguous ingress, path-only validation, unbounded reads, or stale
snapshots acceptable.

## Proposed implementation sequence

### Implementation PR A: privileged filesystem transaction foundation

Keep the first code review narrow:

- a root-only, allowlisted internal/helper entry point with no sudoers and no
  web/CLI mutation exposed to the application user;
- immutable release and adopted-setting revalidation;
- root-to-leaf no-follow descriptor walking;
- PR10 preparation and validation executed inside the privileged boundary;
- stale source and candidate rejection;
- metadata inventory and fail-closed unsupported-metadata policy;
- secure ephemeral backup plus minimal durable journal;
- fixed authoritative `journal` plus non-authoritative `journal.next`, complete
  successor records, monotonic generation/predecessor validation, and one
  publication protocol for every recovery-relevant phase;
- deterministic interrupted-publication recovery and strict valid/recovery-
  required/unverifiable namespace classification under the shared outer lock;
- one recovery-only journal lifecycle: durable cleanup decision, authenticated
  idempotent artifact cleanup, affected-directory fsync, secure journal unlink,
  journal-directory fsync, clean rescan, and only then a logical final result;
- a durably clean journal-namespace gate, including fsync and absence rescan
  when initial lookup returns `ENOENT`, before update/adopt/clear/reconciliation
  may change activation authority;
- integration of that barrier into `cfm-update`, release-switching
  installer/reconciliation, adopted-config adoption, and clear before any
  authority mutation;
- race-aware same-directory commit prototype;
- durable-intent then immediate pre-exchange source/candidate/adopted/service
  revalidation, with a distinct no-service `PRECOMMIT_ABORT` path;
- file/directory fsync ordering;
- filesystem rollback, durable terminal cleanup decisions, idempotent artifact
  cleanup, and crash-recovery classification;
- sanitized result types and extensive temporary-directory/fake tests; and
- no cloudflared service restart/reload and no claim of production activation
  success.

Because a config commit without service integration is not an end-user feature,
PR A should remain unwired or require an explicit root-only test/admin mode that
cannot be mistaken for complete activation. Review must decide which is safer
before merge. No candidate may be committed from dashboard traffic.

Production activation MUST NOT be enabled until every existing root manager
operation that can change the active release, adopted config identity, or
related persistent authority establishes the durably clean journal namespace
under the shared outer lock before it revalidates and changes authority. This
is a required integration invariant, not optional follow-up hardening. If gate
integration is split into a separate prerequisite PR, activation remains
unwired/disabled until that PR is merged and verified.

### Implementation PR B: cloudflared service activation and rollback

- fixed service-control interface and executable/unit identity;
- mandatory precommit `SERVICE_BASELINE_VERIFIED` capture, bounded stability,
  and fail-closed rejection of inactive, failed, mismatched, or unstable
  initial service state;
- durable sanitized baseline facts sufficient to prove baseline-equivalent
  service restoration without storing raw command lines or paths;
- fresh baseline revalidation after durable `CONFIG_COMMITTING`, immediate
  exchange with no intervening work, and post-exchange continuity checks that
  turn any detected/ambiguous asynchronous service change into rollback;
- verified reload support or an explicit restart decision;
- systemd job, active-state, stable MainPID, executable/config identity, and
  readiness checks;
- stability windows and bounded timeouts;
- integration with durable journal recovery;
- restart/reload of restored config on failure;
- distinct config-restored/service-failed outcomes; and
- end-to-end activation/rollback tests using fakes or isolated disposable
  systemd fixtures, never the host's production cloudflared service.

Only after both foundations are reviewed should later PRs address:

- a web-to-privileged-boundary authorization mechanism;
- Cloudflare API credentials and positively owned DNS records;
- full Add/Edit/Enable/Disable/Delete domain transactions and compensation; and
- guarded HTTP/HTMX mutation UX.

This split keeps filesystem race/durability review separate from service health
semantics. Combining them would make failure attribution and security review
substantially harder.

## Adversarial test plan

All filesystem tests use temporary directories and injected fakes. No test
touches `/etc/cloudflared`, the real systemd manager, DNS, or Cloudflare.

| Area | Required cases and assertions |
| --- | --- |
| Adopted authority | no adopted path; detected-only path; adopted setting changed mid-transaction; noncanonical path; leaf/ancestor symlink; unsafe path component; current release mismatch; stale helper process |
| Content staleness | source bytes changed after candidate preparation; same inode truncated/rewritten; changed bytes with restored timestamps; size growth beyond limit; non-UTF-8 replacement; digest mismatch |
| Identity staleness | same bytes but file identity replaced; inode replaced; device/mount identity changed; parent directory renamed/replaced; active leaf removed/recreated; hard-link count changed; file type changed |
| Metadata staleness | UID, GID, or mode changed; parent ownership/mode changed; ACL added; xattr/security label/capability added; immutable/append-only flag; unsupported special bits; metadata changes during hashing |
| Candidate integrity | candidate renamed, unlinked, replaced, chmodded, hard-linked, truncated, or rewritten; directory swapped; retained FD differs from visible name; procfs unavailable; unrelated FD inheritance; candidate on another filesystem; power loss before/after candidate parent-directory fsync and identity recheck; no durable journal references an undurable candidate entry |
| Validation | application parser rejects before external acceptance; exact fixed cloudflared argv; FD-bound path survives ancestor swap; `pass_fds` contains only verified directory FD; `shell=False`; bounded timeout/output; unavailable executable; nonzero result; raw secret output absent from errors |
| No-op | no candidate, backup, journal, command, service action, or active-file write for semantic no-op |
| Locking | second activation rejected; manager update/config action contention; shared outer lock precedes journal-directory verification, clean-namespace proof, authority revalidation, and any activation lock; reverse order rejected; lock symlink/type/owner/mode attacks; crash releases descriptor locks but journal barrier remains; manual edit still detected despite manager lock |
| Persistent recovery barrier | every valid authoritative journal with remaining transaction/recovery/cleanup work blocks release update, installer/reconciliation switching, adopt, and clear; malformed/unknown-schema/impossible authoritative state and unsafe/unknown namespace objects also block; recognized current-plus-staging and staging-only crash shapes are cleaned by the fixed protocol rather than mislabeled ambiguous; cleanup-decision identity mismatch blocks and is never ignored; every rejection leaves current release, adopted setting, journal/artifacts, cloudflared config, and both services byte/state unchanged |
| Journal publication | initial generation 1; complete successor; same transaction ID; generation exactly `current + 1`; predecessor generation/digest; allowlisted `BACKUP_DURABLE` or `CONFIG_COMMITTING` abort transition; other illegal transition; short/failed write; staging fsync failure; parse/reverification failure; rename failure; directory-fsync failure; crash after directory fsync but before published reverification; published reopen/digest/namespace failure; assert no successor-dependent side effect before rename plus directory fsync plus published reverification |
| Interrupted journal publication | crash/power loss halfway through staging write, after staging fsync, immediately before rename, immediately after rename, and after rename before directory fsync; valid old plus valid/malformed staging; invalid old plus valid-looking staging; staging-only initial publication; recognized staging symlink/wrong owner/mode/type/link count; extra/alternate journal object; second recovery process; verify `journal` alone is authority, staging is never promoted, supported cleanup is directory-fsynced, and unknown shapes fail closed |
| Durably clean journal namespace | initially absent fixed journal still requires verified-directory fsync and post-fsync absence rescan; directory/ancestor rename, replacement, or identity change around scan/fsync fails closed; injected fsync/rescan failure blocks update/adopt/clear; cleanup-decision journal with already-absent artifacts authenticates against current authority, repeats affected-directory fsyncs, and retires; crash immediately before unlink, immediately after unlink, after unlink before fsync, after reboot with observed absence, and a second crash all resume deterministically; journal reappearance resumes recovery rather than becoming history; authority change immediately after the clean proof cannot resurrect the old journal or require old authority; no authority action or final result before the clean proof |
| Recovery contention | activation recovery and update/config acquire the same outer lock; recovery holding it blocks authority mutation; update/config holding it blocks recovery until release; after acquisition each rechecks the barrier; repeated rejection and recovery are deterministic and deadlock-free |
| Backup | exact bytes, digest, size, UID/GID/mode metadata; exclusive random/fixed-policy name; `0600` root ownership; partial write; fsync failure; directory-fsync failure; collision/symlink; backup tamper; backup cannot be confused with candidate; secrets never logged |
| Pre-exchange revalidation | `CONFIG_COMMITTING` file/directory fsync occurs before the fresh check; source, candidate, adopted authority, parent/metadata, and complete service baseline are rechecked; any mismatch with exact original active takes `PRECOMMIT_ABORT`; assert no exchange and no service command |
| Precommit abort | durable abort decision precedes artifact deletion; only authenticated transaction artifacts are removed; crash before/after every deletion, affected-directory fsync, journal unlink, journal-directory fsync, and clean rescan resumes idempotently; wrong-identity artifact fails closed; `FAILED_PRECOMMIT` is logical only and requires a durably clean namespace |
| Final asynchronous race | service exits/changes immediately after final check, during exchange, and after exchange; tests and implementation cannot claim atomic liveness coupling; post-commit continuity check detects each case and requires activation rollback |
| Precommit race | replacement before final check; replacement between check and exchange; rename of ancestor; in-place writer holding old FD; unexpected displaced inode; rollback-intent publication failure; post-intent exchange-back failure; candidate mutation during exchange; no compensation while `CONFIG_COMMITTING` remains published; crash recovery distinguishes commit from rollback intent; any possible active-name change uses rollback, never precommit abort; operator state is never silently overwritten |
| Commit durability | unavailable `renameat2`/exchange support fails closed; candidate and source filesystem mismatch; metadata application failure; candidate file-fsync, parent-directory-fsync, and post-fsync identity failures block `BACKUP_DURABLE`; active exchange failure; active verification failure; file-fsync failure; directory-fsync failure; journal update failure at each phase |
| Successful commit | exact candidate at adopted name; exact intended owner/group/mode; supported metadata preserved; displaced source matches snapshot; active directory durable; source was never truncated in place |
| Service baseline | unit missing/not loaded; inactive, failed, activating, deactivating, or unexpected substate; zero/unstable/reused PID; restart loop; wrong executable; wrong adopted-config relationship; readiness failure; stability-window failure; every case rejects before namespace mutation and performs no service action |
| Baseline journal | sanitized enums, PID/start identity, executable identity, adopted-config fingerprint/digest relationship, and stability evidence round-trip; no raw `ExecStart`, paths, YAML, token, command line, stdout, or stderr; baseline change before intent or during the mandatory post-intent pre-exchange recheck rejects |
| Service command | fixed absolute executable and unit; no caller-controlled argv/env; timeout; nonzero systemctl result; active-but-zero PID; PID changes during check; wrong executable/config; restart loop; readiness never arrives; readiness arrives then process dies |
| Rollback | activation failure with exact rollback to baseline-equivalent health; config restored but service recovery fails; backup corrupt/missing before terminal decision; active matches unknown third-party state; metadata restore failure; rollback fsync failure; reverse exchange failure; restoration verification mismatch; distinct sanitized outcomes |
| Commit cleanup | crash before commit decision retains every rollback artifact; durable `COMMIT_CLEANUP_PENDING` precedes deletion; crash before/after each artifact unlink, affected-directory fsync, journal unlink, journal-directory fsync, and clean rescan; existing artifact identity mismatch rejects; already-absent allowlisted artifact resumes safely; no success or persistent `COMMITTED_SUCCESS` journal before namespace cleanliness |
| Rollback cleanup | crash before rollback decision retains every recovery artifact; durable `ROLLBACK_CLEANUP_PENDING` precedes deletion; crash before/after each artifact unlink, affected-directory fsync, journal unlink, journal-directory fsync, and clean rescan; identity mismatch rejects; already-absent allowlisted artifact resumes safely; no verified-rollback result or persistent `FAILED_ROLLED_BACK` journal before namespace cleanliness |
| `CONFIG_COMMITTING` recovery | exact original active selects precommit abort/cleanup with no service action; exact candidate plus exact displaced source can continue committed recovery; exact candidate plus unexpected displaced object requires durable failure/rollback intent before compensation or manual recovery; any other active identity is indeterminate/manual recovery; identical classification after an in-memory `PRE_EXCHANGE_REVALIDATED` crash |
| Crash recovery | every row in the crash matrix; old/candidate/unknown active digest; malformed or impossible journal; stale release/adopted path; journal symlink/permissions/tamper; phase-sensitive required versus cleanup-optional missing artifacts; multiple artifacts; idempotent repeated recovery and retirement; no new transaction or authority change until the journal namespace is durably clean |
| Information safety | secret-looking YAML, paths, stdout/stderr, environment and tokens never appear in exceptions, reprs, logs, journal, CLI safe output, or browser models |
| Scope regression | web routes remain GET-only; Add/Edit/Delete remain disabled; no Cloudflare API/DNS calls; no production config writes from web; no sudoers/unit privilege broadening; no cloudflared service call in PR A |

Tests must inject short writes, `EINTR`/I/O errors where relevant, fsync and
close failures, timeout boundaries, and failures after every durable state
transition. Property/state-machine tests are encouraged for journal transition
legality, but do not replace explicit attack regressions.

## Unresolved decisions and mandatory review gates

The design deliberately leaves these questions open until the corresponding
implementation can prove them:

1. **Commit primitive:** confirm that directory-FD-relative
   `renameat2(RENAME_EXCHANGE)` plus displaced-identity verification meets the
   concurrent-edit contract on every supported filesystem. Prototype and
   adversarially test compensation of an unexpected displaced object only
   after durable rollback intent; this document does not prove the exact
   exchange-back algorithm safe. If the operator object cannot be preserved
   across crashes and races, activation remains disabled; unchecked replace
   is not a fallback.
2. **Metadata support:** decide which ACL/xattr/security-label environments are
   supported. The default is fail closed on nontrivial metadata.
3. **State locations:** choose fixed root-owned backup/journal locations and
   demonstrate permissions, mount assumptions, fsync behavior, and recovery.
4. **PR A exposure:** decide whether filesystem transaction code is entirely
   unwired or has a root-only administrative test command. It cannot be exposed
   to the web service or called a complete activation.
5. **Service operation:** verify cloudflared reload support for the deployed
   version/unit; otherwise use restart. Do not infer support from systemctl
   accepting `reload`.
6. **Readiness:** define a meaningful, bounded signal proving the expected
   cloudflared process is stably using the committed config, including behavior
   during network outages.
7. **Future web authorization:** choose an exact non-wildcard invocation and
   request transport, bind it to the active manager process/release, and review
   HTTP authentication/CSRF/rate limits before adding sudoers or mutation
   routes.
8. **Recovery policy after committed config:** decide when an authenticated
   incomplete transaction may resume service verification versus conservatively
   rolling back. Either path must be deterministic and tested.

No implementation PR may silently resolve these by weakening an invariant.
Its description must list the decision made, evidence/tests supporting it,
deployment implications, and any unsupported host/filesystem state that now
fails closed.
