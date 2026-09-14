"""Safe, project-specific cloudflared configuration errors."""


class CloudflaredConfigError(Exception):
    """Base class for expected configuration loading failures."""


class ConfigFileNotFoundError(CloudflaredConfigError):
    """Raised when the explicitly configured file does not exist."""


class ConfigFileUnreadableError(CloudflaredConfigError):
    """Raised when the configured file cannot be read as text."""


class ConfigInvalidYamlError(CloudflaredConfigError):
    """Raised when safe YAML parsing fails."""


class ConfigStructureError(CloudflaredConfigError):
    """Raised when parsed YAML does not have the required structure."""
