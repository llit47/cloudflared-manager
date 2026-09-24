"""Candidate-only orchestration that deliberately stops before activation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from cloudflared_manager.cloudflared import (
    CloudflaredConfigError,
    parse_cloudflared_config,
)
from cloudflared_manager.cloudflared.editing.candidate import (
    CandidateFile,
    CandidateFileStager,
)
from cloudflared_manager.cloudflared.editing.document import (
    EditableCloudflaredConfig,
    MutationOutcome,
)
from cloudflared_manager.cloudflared.editing.errors import (
    ApplicationValidationError,
    CandidateFileError,
    MutationRejectedError,
)
from cloudflared_manager.cloudflared.editing.source import (
    ConfigSourceSnapshot,
    read_config_source_snapshot,
    require_source_unchanged,
)
from cloudflared_manager.cloudflared.editing.validation import (
    CandidateValidator,
    CloudflaredCandidateValidator,
    CloudflaredValidationReport,
)

ConfigMutation = Callable[[EditableCloudflaredConfig], MutationOutcome]
ApplicationParser = Callable[[Path], Any]


class PreparationOutcome(StrEnum):
    NO_CHANGE = "no_change"
    VALIDATED_CANDIDATE = "validated_candidate"


@dataclass(frozen=True, slots=True, repr=False)
class CandidatePreparationResult:
    """An explicit no-op or a validated candidate that still cannot activate itself."""

    outcome: PreparationOutcome
    source: ConfigSourceSnapshot
    candidate: CandidateFile | None = None
    cloudflared_validation: CloudflaredValidationReport | None = None

    def __repr__(self) -> str:
        return f"CandidatePreparationResult(outcome={self.outcome!r})"

    @property
    def changed(self) -> bool:
        return self.outcome is PreparationOutcome.VALIDATED_CANDIDATE

    def discard(self) -> None:
        if self.candidate is not None:
            self.candidate.discard()


def prepare_validated_candidate(
    source_path: str | Path,
    mutation: ConfigMutation,
    *,
    stager: CandidateFileStager | None = None,
    application_parser: ApplicationParser = parse_cloudflared_config,
    cloudflared_validator: CandidateValidator | None = None,
    expected_source_revision: str | None = None,
) -> CandidatePreparationResult:
    """Prepare and validate a candidate, then stop without active-file mutation."""

    snapshot = read_config_source_snapshot(source_path)
    if expected_source_revision is not None:
        from cloudflared_manager.cloudflared.editing.errors import StaleMutationError
        if snapshot.sha256 != expected_source_revision:
            raise StaleMutationError("The observed configuration revision is stale.")
    document = EditableCloudflaredConfig.from_snapshot(snapshot)
    outcome = mutation(document)
    if outcome is MutationOutcome.NO_CHANGE:
        if document.changed:
            raise MutationRejectedError(
                "A mutation reported no change after modifying the document."
            )
        require_source_unchanged(snapshot)
        return CandidatePreparationResult(PreparationOutcome.NO_CHANGE, snapshot)
    if outcome is not MutationOutcome.CHANGED or not document.changed:
        raise MutationRejectedError(
            "A mutation returned an inconsistent change result."
        )

    rendered = document.render_changed()
    require_source_unchanged(snapshot)
    candidate = (stager or CandidateFileStager()).stage(snapshot, rendered)
    try:
        binding = candidate.validation_binding()
        try:
            application_parser(binding.path)
        except CloudflaredConfigError as error:
            raise ApplicationValidationError(
                "The application parser rejected the candidate configuration."
            ) from error
        candidate.require_intact()
        report = (cloudflared_validator or CloudflaredCandidateValidator()).validate(
            candidate
        )
        if not report.accepted:
            raise MutationRejectedError(
                "The external validator returned an inconsistent result."
            )
        require_source_unchanged(snapshot)
        candidate.require_intact()
    except Exception as error:
        try:
            candidate.discard()
        except CandidateFileError as cleanup_error:
            raise cleanup_error from error
        raise

    return CandidatePreparationResult(
        PreparationOutcome.VALIDATED_CANDIDATE,
        snapshot,
        candidate,
        report,
    )
