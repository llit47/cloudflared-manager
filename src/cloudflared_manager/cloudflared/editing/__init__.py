"""Internal candidate-generation foundation with no activation capability."""

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
    CloudflaredValidationExecutionError,
    CloudflaredValidationRejectedError,
    CloudflaredValidationTimeoutError,
    CloudflaredValidatorUnavailableError,
    ConfigEditingError,
    MutationRejectedError,
    RoundTripYamlError,
    SourceConfigChangedError,
    SourceConfigUnreadableError,
    UnsupportedConfigStructureError,
)
from cloudflared_manager.cloudflared.editing.preparation import (
    CandidatePreparationResult,
    PreparationOutcome,
    prepare_validated_candidate,
)
from cloudflared_manager.cloudflared.editing.source import (
    ConfigSourceSnapshot,
    read_config_source_snapshot,
    require_source_unchanged,
)
from cloudflared_manager.cloudflared.editing.validation import (
    CloudflaredCandidateValidator,
    CloudflaredValidationReport,
    SubprocessValidationCommandRunner,
    ValidationCommandResult,
)

__all__ = [
    "ApplicationValidationError",
    "CandidateFile",
    "CandidateFileError",
    "CandidateFileStager",
    "CandidatePreparationResult",
    "CloudflaredCandidateValidator",
    "CloudflaredValidationExecutionError",
    "CloudflaredValidationRejectedError",
    "CloudflaredValidationReport",
    "CloudflaredValidationTimeoutError",
    "CloudflaredValidatorUnavailableError",
    "ConfigEditingError",
    "ConfigSourceSnapshot",
    "EditableCloudflaredConfig",
    "MutationOutcome",
    "MutationRejectedError",
    "PreparationOutcome",
    "RoundTripYamlError",
    "SourceConfigChangedError",
    "SourceConfigUnreadableError",
    "SubprocessValidationCommandRunner",
    "UnsupportedConfigStructureError",
    "ValidationCommandResult",
    "prepare_validated_candidate",
    "read_config_source_snapshot",
    "require_source_unchanged",
]
