"""Service phases of the internal transaction, using PR12 authentication."""
from __future__ import annotations

from typing import Protocol

from cloudflared_manager.activation.filesystem import FilesystemRefused
from cloudflared_manager.activation.journal import BaselineFacts


class ServiceController(Protocol):
    def observe(self, *, adopted_fingerprint: str, source_digest: str) -> BaselineFacts: ...
    def settled(self) -> bool: ...
    def restart(self) -> bool: ...
    def verify(self, baseline: BaselineFacts, *, activation: bool) -> None: ...


def resume_service(engine, record, journal, active, backups, active_name, *, recovery=False):
    """Every action follows a completed, authenticated publication.

    Publication/authority failures propagate without selecting another service
    action. Durable verification phases never consult the live service.
    """
    def publish(phase, **kwargs):
        return journal.publish(
            record.successor(phase, **kwargs),
            authenticate=lambda item: engine._authenticate(item, active, backups),
            authenticate_complete_cleanup=lambda item: engine._authenticate_complete(item, active, backups),
        )

    engine._authenticate(record, active, backups)
    if record.phase == "CONFIG_COMMITTED":
        if not engine.service.settled():
            return "RECOVERY_REQUIRED"
        if not recovery:
            try:
                observed = engine.baseline.observe(
                    adopted_fingerprint=record.adopted_fingerprint,
                    source_digest=record.source.sha256,
                )
                if observed != record.baseline:
                    raise FilesystemRefused("BASELINE_CHANGED")
            except Exception:
                return engine._handle_failure(record, journal, active, backups, active_name)
        record = publish("SERVICE_ACTIVATING")
    elif record.phase == "ROLLBACK_CONFIG":
        record = publish("ROLLBACK_SERVICE")

    if record.phase in {"SERVICE_ACTIVATING", "ROLLBACK_SERVICE"}:
        rollback = record.phase == "ROLLBACK_SERVICE"
        if not engine.service.settled():
            return "RECOVERY_REQUIRED"
        # Observation can take time. Authenticate config/authority again before
        # the command, while the durably reverified phase remains authority.
        engine._authenticate(record, active, backups)
        success = engine.service.restart()
        if success:
            try:
                engine.service.verify(record.baseline, activation=not rollback)
            except Exception:
                success = False
        if not success:
            if not engine.service.settled():
                return "RECOVERY_REQUIRED"
            if rollback:
                return "CONFIG_RESTORED_SERVICE_RECOVERY_FAILED"
            return engine._handle_failure(record, journal, active, backups, active_name)
        record = publish("ROLLBACK_VERIFIED" if rollback else "SERVICE_VERIFIED")

    if record.phase in {"SERVICE_VERIFIED", "ROLLBACK_VERIFIED"}:
        rollback = record.phase == "ROLLBACK_VERIFIED"
        record = publish("ROLLBACK_CLEANUP_PENDING" if rollback else "COMMIT_CLEANUP_PENDING",
                         cleanup={"candidate": record.candidate if rollback else record.source,
                                  "backup": record.backup})
    if record.phase in {"COMMIT_CLEANUP_PENDING", "ROLLBACK_CLEANUP_PENDING"}:
        engine._finish_cleanup(record, journal, active, backups)
        return "COMMITTED_SUCCESS" if record.phase == "COMMIT_CLEANUP_PENDING" else "FAILED_ROLLED_BACK"
    raise FilesystemRefused("RECOVERY_REQUIRED")
