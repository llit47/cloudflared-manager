"""Sanitized errors raised by production deployment operations."""


class DeploymentError(RuntimeError):
    """Base class for an expected deployment failure."""


class ValidationError(DeploymentError):
    """A deployment input failed strict validation."""


class NetworkSelectionError(DeploymentError):
    """A single safe LAN bind address could not be selected."""


class EnvironmentFileError(DeploymentError):
    """The manager environment file was unsafe or malformed."""


class SourceError(DeploymentError):
    """A source revision could not be resolved, downloaded, or unpacked."""


class HealthCheckError(DeploymentError):
    """The manager did not satisfy its health contract in time."""


class HostOperationError(DeploymentError):
    """A fixed local deployment operation failed."""


class UpdateLockedError(DeploymentError):
    """Another update currently owns the deployment lock."""


class RollbackError(DeploymentError):
    """A failed change could not restore a healthy previous deployment."""


class TransactionFailedError(DeploymentError):
    """A requested change failed after the previous state was restored."""
