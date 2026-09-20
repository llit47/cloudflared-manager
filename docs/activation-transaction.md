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
- **Transaction journal**: the minimal root-owned durable record that identifies
  an incomplete transaction and its expected source, candidate, backup, and
  phase. It must not contain raw YAML or credentials.
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
25. Rollback/recovery artifacts are never deleted before a durable terminal
    commit or rollback decision. Cleanup after that decision is authenticated,
    idempotent, directory-fsynced, and complete before a user-visible terminal
    result.

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
  -> CONFIG_COMMITTED
  -> SERVICE_ACTIVATING
  -> SERVICE_VERIFIED
  -> COMMIT_CLEANUP_PENDING
  -> COMMITTED_SUCCESS

Failure after CONFIG_COMMITTING:

ACTIVATION_FAILED
  -> ROLLBACK_CONFIG
  -> ROLLBACK_SERVICE
  -> ROLLBACK_VERIFIED
  -> ROLLBACK_CLEANUP_PENDING
  -> FAILED_ROLLED_BACK

Any unverified restoration or service recovery:

ROLLBACK_FAILED
```

`CONFIG_COMMITTING` is included because a crash can occur during the atomic
namespace operation before a later journal update. `BACKUP_DURABLE` is the last
state that is still purely read-only with respect to the active name. The
atomic exchange/replace that leaves candidate bytes at the active name is the
point of no longer purely read-only staging.

`SERVICE_BASELINE_VERIFIED` is a mandatory precommit gate, not merely an
observation for diagnostics. The first production implementation accepts only
a loaded, active, stably running cloudflared service whose process/executable
identity and relationship to the adopted config are verified. An inactive,
failed, activating, deactivating, restart-looping, path-mismatched, or otherwise
unhealthy service causes rejection before `CONFIG_COMMITTING`; the transaction
does not attempt to turn that state into a supported baseline.

`COMMIT_CLEANUP_PENDING` and `ROLLBACK_CLEANUP_PENDING` are durable terminal
decisions. Once either is fsynced, the selected outcome no longer depends on
rollback artifacts remaining present. Cleanup can then remove authenticated
artifacts idempotently. User-visible success or verified-rollback failure is
not returned until cleanup, affected-directory fsync, and the corresponding
durable terminal journal state are complete.

A cleanup error after either durable decision does not reverse that decision.
The journal remains in its cleanup-pending state, no new transaction may begin,
and root recovery resumes the same idempotent cleanup. An existing artifact
with the wrong identity is not treated as already cleaned and requires manual
intervention; an allowlisted artifact that is absent is safe to skip.

### State and transition contract

| Transition | Prerequisites and verification | Side effects and durable state | Failure behavior |
| --- | --- | --- | --- |
| `IDLE -> SOURCE_VERIFIED` | Lock held; no incomplete journal; helper/release and adopted path independently valid; canonical path opened without symlinks; bounded snapshot succeeds | Read-only descriptors and immutable snapshot only | Close descriptors; sanitized rejection; no rollback |
| `SOURCE_VERIFIED -> CANDIDATE_PREPARED` | Narrow mutation accepts structure and reports a real change | Exclusive random `0600` same-directory candidate, complete write and candidate file `fsync`; retained file/directory identity | Remove only the verified candidate; source untouched |
| `CANDIDATE_PREPARED -> CANDIDATE_VALIDATED` | Candidate identity intact | Existing application parser succeeds, then fixed cloudflared ingress validation succeeds through FD-bound path; sanitized report retained | Discard candidate; source untouched |
| `CANDIDATE_VALIDATED -> SERVICE_BASELINE_VERIFIED` | Unit is loaded; state is active with the expected running substate; positive MainPID, process start identity, executable identity, adopted-config relationship, and readiness remain stable across a bounded observation | Read-only checks only; sanitized baseline facts retained in memory | Reject before active mutation; discard candidate; do not start/restart/reload an unhealthy service |
| `SERVICE_BASELINE_VERIFIED -> PRECOMMIT_REVALIDATED` | Baseline is still current; candidate rechecked; adopted setting reread; active path/parent reopened through no-follow descriptor walk; source bytes and metadata compared with original snapshot | Read-only checks only | Reject stale source or service state; never merge/rebase; discard candidate |
| `PRECOMMIT_REVALIDATED -> BACKUP_DURABLE` | Source still bound by retained descriptors; service baseline remains valid; backup/journal storage verified root-owned and restrictive | Exact source bytes copied from verified descriptor with pre/post identity checks; restoration metadata and digests recorded; backup file and directory fsynced; minimal journal atomically records identities, sanitized baseline facts, and `BACKUP_DURABLE`, then its directory is fsynced | Remove incomplete artifacts only when identity is proven; source untouched |
| `BACKUP_DURABLE -> CONFIG_COMMITTING` | Final candidate/source/adopted-path checks; live service still matches the verified baseline; journal already contains baseline and exact artifact identities; commit primitive available | Journal durably records intent to modify active name | On failure before namespace change, recover as precommit and leave source untouched |
| `CONFIG_COMMITTING -> CONFIG_COMMITTED` | Race-aware same-directory commit; displaced active object verified as exact expected source; new active object verified as exact candidate with intended metadata | One atomic namespace commit, then active file fsync as applicable and parent directory fsync; journal advances only after durability | If displaced source differs, immediately restore/exchange it and return stale rejection; after any active-name change, rollback is mandatory |
| `CONFIG_COMMITTED -> SERVICE_ACTIVATING` | Active bytes/digest, metadata, and adopted name reverified; service phase implemented | Fixed allowlisted restart or verified reload begins; journal records phase durably first | Enter `ACTIVATION_FAILED`; rollback required |
| `SERVICE_ACTIVATING -> SERVICE_VERIFIED` | systemd command succeeded and bounded readiness checks prove expected service/process/config stability | Read-only service observations; journal records verification evidence without secrets | Enter `ACTIVATION_FAILED`; rollback required while no commit decision exists |
| `SERVICE_VERIFIED -> COMMIT_CLEANUP_PENDING` | Active candidate and service stability reverified; success is now irrevocably selected | Journal durably records commit decision, complete cleanup allowlist, and `COMMIT_CLEANUP_PENDING` before any rollback artifact is removed | Journal/fsync failure leaves artifacts intact and no user-visible success; recovery still follows pre-decision rules |
| `COMMIT_CLEANUP_PENDING -> COMMITTED_SUCCESS` | Durable commit decision authentic; each existing cleanup artifact matches its journaled identity | Remove authenticated artifacts idempotently, tolerate already-absent allowlisted artifacts, fsync every affected directory, then durably record `COMMITTED_SUCCESS` | Resume cleanup on restart; never roll back solely because a cleanup artifact is absent; do not report success until terminal durability completes |
| `ACTIVATION_FAILED -> ROLLBACK_CONFIG` | Durable backup and/or retained displaced original authenticated against journal | Restore exact old bytes/metadata using secure same-directory staging and atomic namespace operation; fsync file and directory | Any uncertainty becomes `ROLLBACK_FAILED` |
| `ROLLBACK_CONFIG -> ROLLBACK_SERVICE` | Old config digest and metadata verified at adopted name | Fixed service activation for restored config | Distinguish config-restored/service-unrecovered outcome |
| `ROLLBACK_SERVICE -> ROLLBACK_VERIFIED` | Old file is exact; service is again loaded, active, ready, and stably equivalent to the journaled healthy baseline, with the expected executable/config relationship | Read-only verification; durable journal update | Failure becomes a distinct config-restored/service-recovery or rollback failure |
| `ROLLBACK_VERIFIED -> ROLLBACK_CLEANUP_PENDING` | Exact old config and a healthy baseline-equivalent service are proven; rollback is irrevocably selected | Journal durably records rollback-complete decision, cleanup allowlist, and `ROLLBACK_CLEANUP_PENDING` before artifact deletion | Journal/fsync failure leaves artifacts intact and rollback outcome unfinalized |
| `ROLLBACK_CLEANUP_PENDING -> FAILED_ROLLED_BACK` | Durable rollback decision authentic; each existing cleanup artifact matches its journaled identity | Remove authenticated artifacts idempotently, tolerate already-absent allowlisted artifacts, fsync every affected directory, then durably record `FAILED_ROLLED_BACK` | Resume cleanup on restart; missing allowlisted artifacts alone are not indeterminate; return activation failure only after terminal durability |

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
object is the exact snapshotted source. If it is not, the helper treats the
operation as stale interference and immediately exchanges/restores the
displaced object; it never treats the candidate as committed. The displaced
original also provides the most exact immediate rollback object, while the
separate durable backup covers crash recovery.

This is compare-and-verify, not a true kernel compare-and-swap on destination
inode. A cooperating manager lock plus exchange minimizes the gap, but an
independent root process can still race any userspace protocol. Implementation
PR A MUST prototype the exchange and adversarially test rename, replacement,
and open-file writes. If it cannot prove that an unexpected displaced file is
restored without losing the operator's state, production activation remains
disabled. Falling back to unchecked `os.replace` is not allowed.

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
again, and use a separate commit-stage verifier. It must not weaken PR10's
pre-validation `0600` rule or accidentally call the old verifier after changing
the mode.

The ordering for a successful filesystem commit is:

1. fsync the complete candidate;
2. create and fsync the exact backup and metadata;
3. fsync the backup directory;
4. atomically write/fsync the journal state identifying source, candidate, and
   backup;
5. fsync the journal directory;
6. perform the race-aware same-directory namespace exchange;
7. verify both the new active file and displaced source identities;
8. fsync the new active file if the platform/filesystem requires reopening it;
9. fsync the active parent directory;
10. atomically advance and fsync the journal before service activation.

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

On verified success, the journal first durably records
`COMMIT_CLEANUP_PENDING`, including the exact allowlist and identities of
artifacts to remove. Only then may the ephemeral backup and other rollback
artifacts be removed. Each deletion is idempotent: an existing object must
match its journaled identity before deletion, while an already-absent
allowlisted object is expected after a cleanup crash. Every affected directory
is fsynced before `COMMITTED_SUCCESS` is recorded durably and success is
reported.

After verified rollback, the same ordering applies through
`ROLLBACK_CLEANUP_PENDING` before artifacts are removed and
`FAILED_ROLLED_BACK` is recorded durably. Before either cleanup-pending decision,
a missing required backup remains a rollback/recovery failure. On rollback
failure or ambiguous pre-decision recovery, artifacts are retained for root
administrator recovery. Automatic age-based deletion MUST NOT remove an
artifact referenced by a nonterminal journal.

Bounded historical backups are not required for the first implementation.
They increase secret retention and require a separate retention/audit policy.
Operators who need historical configuration should use an independently
secured configuration-management system.

## Service activation and readiness model

### Required healthy baseline

The first production activation implementation supports only a service that is
already healthy. Before `PRECOMMIT_REVALIDATED`, and again immediately before
`CONFIG_COMMITTING`, the helper MUST verify a `SERVICE_BASELINE_VERIFIED`
baseline. At minimum it requires:

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
wrong-config, unstable, or unverifiable service fails closed before active-file
namespace mutation. The helper does not start, restart, reload, or repair it as
part of the first activation design. The administrator must establish a healthy
baseline independently and begin a new transaction.

The `BACKUP_DURABLE` journal record, which is durable before
`CONFIG_COMMITTING`, captures only sanitized baseline facts: an allowlisted unit
identity, normalized load/active/substate enums, baseline MainPID and
process-start identity, executable device/inode and/or trusted digest/version
identity, an adopted-config fingerprint and source digest relationship, the
bounded stability duration/result, and any bounded restart-counter/timestamp
facts required to detect instability. It contains no raw `ExecStart`, config or
executable path, command line, environment, token, YAML, stdout, or stderr.

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

Any failure after the active namespace might have changed enters rollback. The
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
- `ACTIVATION_FAILED_ROLLBACK_VERIFIED`: activation failed; exact prior config
  and a healthy service state equivalent to the captured baseline were proven
  restored, and rollback cleanup reached its durable terminal state;
- `CONFIG_RESTORED_SERVICE_RECOVERY_FAILED`: exact config restoration was
  proven but cloudflared could not be returned to verified health;
- `ROLLBACK_PARTIAL_FAILURE`: some restoration step failed and current state is
  known but not fully restored;
- `ROLLBACK_FAILED_STATE_INDETERMINATE`: current file/service state cannot be
  safely classified; automatic mutation stops; and
- `TERMINAL_CLEANUP_REQUIRED`: commit or verified rollback has a durable
  decision, but authenticated cleanup or terminal journal durability is not
  complete; it is neither user-visible success nor a new rollback decision;
  and
- `RECOVERY_REQUIRED`: a durable incomplete journal was found before a new
  transaction.

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
version, transaction ID, phase, release ID, non-secret adopted-path fingerprint,
source/candidate/backup digests and sizes, required identity/metadata facts,
sanitized `SERVICE_BASELINE_VERIFIED` facts, and artifact names chosen by the
helper. Cleanup-pending records additionally contain the complete allowlist and
expected identities of artifacts eligible for deletion. The journal is
root-owned `0600`, bounded, strictly parsed, atomically rewritten, file-fsynced,
and directory-fsynced at each recovery-relevant transition.

The journal is not trusted merely because it is root-owned. Recovery verifies
every referenced artifact through fixed directories, strict leaf-name grammar,
no-follow descriptors, digest/size/metadata checks, and current adopted/release
identity. Unknown schema, duplicate fields, malformed content, impossible
transition, unsafe permissions, identity mismatch, or multiple journals cause
fail-closed manual recovery. No artifact path from the journal may escape its
fixed directory.

Artifact presence is interpreted by phase. Before a durable terminal decision,
every artifact required for rollback must exist and authenticate; absence is a
rollback/recovery failure. In `COMMIT_CLEANUP_PENDING` or
`ROLLBACK_CLEANUP_PENDING`, the journal proves that rollback or commit has
already been selected and verified. Existing allowlisted artifacts must still
authenticate before deletion, but missing allowlisted artifacts mean cleanup
already progressed and are not by themselves indeterminate. Recovery resumes
cleanup, fsyncs affected directories, and writes the corresponding durable
terminal state. A durable `COMMITTED_SUCCESS` or `FAILED_ROLLED_BACK` record can
be retained as the bounded single terminal record and replaced only when a new
transaction safely begins; deleting the journal is not required to report the
terminal result.

### Crash matrix

| Crash point | Durable state that may remain | Required next-run behavior |
| --- | --- | --- |
| Before candidate creation | Source only; no journal | Normal start; nothing to recover |
| During candidate write, before candidate fsync | Incomplete hidden candidate | Never activate it; remove only after name/inode/type/ownership checks, otherwise fail closed |
| After candidate fsync or validation | Valid disposable candidate; active unchanged; normally no journal | Revalidate and explicitly resume only within same live transaction; after process loss, securely discard it |
| During or after service baseline observation, before durable journal | Active config/service unchanged; candidate may remain; no durable baseline authority | Securely discard candidate after identity checks; a new transaction must establish a fresh bounded healthy baseline |
| Baseline is unhealthy or becomes unstable | Active config namespace unchanged | Fail closed with `SERVICE_BASELINE_UNAVAILABLE`; never start/restart/reload service and never enter commit |
| During backup write, before backup fsync | Incomplete backup; active unchanged | Journal must not claim `BACKUP_DURABLE`; remove authenticated incomplete artifacts or require manual recovery on mismatch |
| After backup fsync but before backup-directory fsync | Backup existence is not durable | Active unchanged; repeat/clean backup creation; do not commit |
| After durable backup but before journal fsync | Active unchanged; orphan durable backup possible | Discover only through fixed bounded artifact policy; never infer authority from filename alone; clean if identity can be proven |
| After durable `BACKUP_DURABLE` journal, before commit intent | Active unchanged; backup, candidate identities, and sanitized healthy baseline are journaled | Revalidate source/candidate and establish that the live service still matches the recorded baseline; safe recovery may abort and clean without touching active |
| After durable `CONFIG_COMMITTING`, before exchange | Active normally old, but crash timing is uncertain; prior healthy baseline is durable | Compare active and artifact digests/identities: old source means abort/clean; candidate at active means continue recovery using journaled baseline; anything else requires manual intervention |
| Immediately after atomic exchange | Candidate may be active; old source at transaction name; directory change may not be durable | Use journal plus identities to classify; fsync/restore according to conservative recovery; never start a new transaction |
| After active file fsync but before active-directory fsync | File data durable; name swap may not be | Same classification; do not assume either namespace survived power loss |
| After active-directory fsync but before `CONFIG_COMMITTED` journal | New active is durable; journal says committing | Verify candidate digest at active and exact old source/backup, then advance to recovery/service phase or rollback according to implementation policy |
| After `CONFIG_COMMITTED` but before service operation | New active durable; old process may still use old config | Resume bounded service activation when PR B supports it; PR A must report recovery required rather than success |
| During restart/reload | New active durable; service state unknown | Reinspect systemd and process identity; verify readiness or roll back; do not trust prior command status |
| After service becomes healthy but before `SERVICE_VERIFIED` journal | New active and possibly healthy service; journal incomplete | Repeat idempotent readiness/stability verification; success may be recorded only after all identities match |
| After `SERVICE_VERIFIED` but before commit decision | Successful active/service state plus all required rollback artifacts | Do not delete artifacts; reverify active/service state, then durably choose `COMMIT_CLEANUP_PENDING` or roll back if verification fails |
| After durable `COMMIT_CLEANUP_PENDING`, before or during cleanup | Commit is irrevocably selected; some or all rollback artifacts may remain | Never roll back solely for missing cleanup artifacts; authenticate and remove those still present, accept already-absent allowlisted artifacts, and fsync affected directories |
| After commit cleanup directory fsync but before `COMMITTED_SUCCESS` | Commit decision and completed cleanup are durable, journal still says cleanup pending | Repeat idempotent absence/authentication checks and directory fsync, then durably record `COMMITTED_SUCCESS`; do not report success earlier |
| After durable `COMMITTED_SUCCESS` | New config/service verified; rollback artifacts absent; bounded terminal journal remains | Return/reconstruct success safely; the terminal record may be replaced only when a new transaction begins under the lock |
| During rollback staging or namespace restoration | Journal says rollback; active may be candidate or restored source | Classify only known digests and artifacts; continue exact restoration if safe; otherwise `ROLLBACK_FAILED_STATE_INDETERMINATE` |
| After restored file fsync but before directory fsync | Restored bytes exist but namespace durability uncertain | Repeat classification and required fsync; service recovery waits for durable namespace |
| During rollback service recovery | Old config durable; service state unknown | Verify/retry bounded fixed activation and readiness; distinguish config-restored/service-failed |
| After `ROLLBACK_VERIFIED` but before rollback decision | Old config and baseline-equivalent service proven; all required recovery artifacts remain | Do not delete artifacts; durably record `ROLLBACK_CLEANUP_PENDING` first |
| After durable `ROLLBACK_CLEANUP_PENDING`, before or during cleanup | Verified rollback is irrevocably selected; artifacts may be partially absent | Authenticate and remove remaining allowlisted artifacts idempotently, accept already-absent allowlisted artifacts, and fsync affected directories |
| After rollback cleanup directory fsync but before `FAILED_ROLLED_BACK` | Restored config/service and cleanup are durable; journal still says cleanup pending | Repeat idempotent cleanup verification and fsync, then durably record `FAILED_ROLLED_BACK`; only then return verified-rollback failure |
| After durable `FAILED_ROLLED_BACK` | Exact old config and baseline-equivalent service restored; rollback artifacts absent; bounded terminal journal remains | Return/reconstruct the verified-rollback failure; a later transaction may replace the terminal record under the lock |

Recovery is invoked explicitly inside the privileged boundary before any new
transaction. Merely starting the web service MUST NOT mutate cloudflared or
auto-recover an incomplete transaction. An implementation may provide a
root-only status/recover operation, but it cannot offer arbitrary artifact
selection.

## Locking and concurrency

Only one cloudflared activation transaction may run at a time. The privileged
component holds a non-blocking exclusive advisory lock in a fixed root-owned
`0700` runtime or persistent state directory for the entire prepare, validate,
commit, service, and rollback sequence. The lock file is fixed, no-follow,
regular, root-owned, `0600`, and descriptor-held.

The current manager deployment/configuration lock protects updates and adopted
setting changes. Activation needs coordination with it because changing the
adopted path or manager release during a transaction invalidates authority.
The implementation should either use one documented global lock order or a
single shared outer manager-operation lock. Proposed order if two locks remain:

1. acquire the existing manager deployment/config lock;
2. verify current release and adopted setting;
3. acquire the cloudflared activation lock;
4. hold both until transaction or rollback is complete.

No code may acquire them in reverse order. Long service readiness waits make a
single shared lock simpler and safer unless review shows unacceptable impact.
The lock scope and order must be tested for update/config/activation contention.

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
The audit record is not used as transaction authority.

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
- race-aware same-directory commit prototype;
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

### Implementation PR B: cloudflared service activation and rollback

- fixed service-control interface and executable/unit identity;
- mandatory precommit `SERVICE_BASELINE_VERIFIED` capture, bounded stability,
  and fail-closed rejection of inactive, failed, mismatched, or unstable
  initial service state;
- durable sanitized baseline facts sufficient to prove baseline-equivalent
  service restoration without storing raw command lines or paths;
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
| Candidate integrity | candidate renamed, unlinked, replaced, chmodded, hard-linked, truncated, or rewritten; directory swapped; retained FD differs from visible name; procfs unavailable; unrelated FD inheritance; candidate on another filesystem |
| Validation | application parser rejects before external acceptance; exact fixed cloudflared argv; FD-bound path survives ancestor swap; `pass_fds` contains only verified directory FD; `shell=False`; bounded timeout/output; unavailable executable; nonzero result; raw secret output absent from errors |
| No-op | no candidate, backup, journal, command, service action, or active-file write for semantic no-op |
| Locking | second activation rejected; manager update/config action contention; defined lock order; lock symlink/type/owner/mode attacks; lock released after every failure; manual edit still detected despite manager lock |
| Backup | exact bytes, digest, size, UID/GID/mode metadata; exclusive random/fixed-policy name; `0600` root ownership; partial write; fsync failure; directory-fsync failure; collision/symlink; backup tamper; backup cannot be confused with candidate; secrets never logged |
| Precommit race | replacement before final check; replacement between check and exchange; rename of ancestor; in-place writer holding old FD; unexpected displaced inode; exchange-back failure; candidate mutation during exchange; operator state is never silently overwritten |
| Commit durability | unavailable `renameat2`/exchange support fails closed; candidate and source filesystem mismatch; metadata application failure; active exchange failure; active verification failure; file-fsync failure; directory-fsync failure; journal update failure at each phase |
| Successful commit | exact candidate at adopted name; exact intended owner/group/mode; supported metadata preserved; displaced source matches snapshot; active directory durable; source was never truncated in place |
| Service baseline | unit missing/not loaded; inactive, failed, activating, deactivating, or unexpected substate; zero/unstable/reused PID; restart loop; wrong executable; wrong adopted-config relationship; readiness failure; stability-window failure; every case rejects before namespace mutation and performs no service action |
| Baseline journal | sanitized enums, PID/start identity, executable identity, adopted-config fingerprint/digest relationship, and stability evidence round-trip; no raw `ExecStart`, paths, YAML, token, command line, stdout, or stderr; live baseline change before `CONFIG_COMMITTING` rejects |
| Service command | fixed absolute executable and unit; no caller-controlled argv/env; timeout; nonzero systemctl result; active-but-zero PID; PID changes during check; wrong executable/config; restart loop; readiness never arrives; readiness arrives then process dies |
| Rollback | activation failure with exact rollback to baseline-equivalent health; config restored but service recovery fails; backup corrupt/missing before terminal decision; active matches unknown third-party state; metadata restore failure; rollback fsync failure; reverse exchange failure; restoration verification mismatch; distinct sanitized outcomes |
| Commit cleanup | crash before commit decision retains every rollback artifact; durable `COMMIT_CLEANUP_PENDING` precedes deletion; crash before/after each unlink and directory fsync; existing artifact identity mismatch rejects; already-absent allowlisted artifact resumes safely; no success before durable `COMMITTED_SUCCESS` |
| Rollback cleanup | crash before rollback decision retains every recovery artifact; durable `ROLLBACK_CLEANUP_PENDING` precedes deletion; crash before/after each unlink and directory fsync; identity mismatch rejects; already-absent allowlisted artifact resumes safely; no verified-rollback result before durable `FAILED_ROLLED_BACK` |
| Crash recovery | every row in the crash matrix; old/candidate/unknown active digest; malformed or impossible journal; stale release/adopted path; journal symlink/permissions/tamper; phase-sensitive required versus cleanup-optional missing artifacts; multiple artifacts; idempotent repeated recovery; no new transaction while recovery is incomplete |
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
   concurrent-edit contract on every supported filesystem. If not, activation
   remains disabled; unchecked replace is not a fallback.
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
