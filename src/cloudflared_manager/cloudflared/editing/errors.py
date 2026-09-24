"""Sanitized failures for candidate-only cloudflared configuration editing."""


class ConfigEditingError(Exception):
    """Base class for expected candidate preparation failures."""


class SourceConfigUnreadableError(ConfigEditingError):
    """The source configuration could not be snapshotted safely."""


class SourceConfigChangedError(ConfigEditingError):
    """The source configuration changed while a candidate was prepared."""


class RoundTripYamlError(ConfigEditingError):
    """The source is not supported, well-formed UTF-8 YAML."""


class UnsupportedConfigStructureError(ConfigEditingError):
    """The editable YAML does not satisfy required structural assumptions."""


class MutationRejectedError(ConfigEditingError):
    """A requested controlled mutation is unsafe or internally inconsistent."""


class StaleMutationError(MutationRejectedError):
    """The observed source revision or selected ingress rule is no longer current."""


class CandidateFileError(ConfigEditingError):
    """A candidate file could not be created, verified, or cleaned safely."""


class ApplicationValidationError(ConfigEditingError):
    """The existing application parser rejected a candidate."""


class CloudflaredValidatorUnavailableError(ConfigEditingError):
    """The cloudflared executable is unavailable for validation."""


class CloudflaredValidationTimeoutError(ConfigEditingError):
    """Cloudflared validation exceeded its bounded timeout."""


class CloudflaredValidationExecutionError(ConfigEditingError):
    """Cloudflared validation could not be executed safely."""


class CloudflaredValidationRejectedError(ConfigEditingError):
    """Cloudflared returned a non-zero result for a candidate."""
