class CompanyOSError(Exception):
    """Base error for expected AI Company OS failures."""


class NotFoundError(CompanyOSError):
    """A requested canonical record does not exist."""


class ValidationError(CompanyOSError):
    """A structured input failed deterministic validation."""


class ConflictError(CompanyOSError):
    """An idempotency key or council contribution conflicts."""


class CompanyStoppedError(CompanyOSError):
    """Execution is disabled until the CEO resumes the company."""


class VerifierTamperError(CompanyOSError):
    """The verifier changed after the WorkOrder was approved."""


class StaleExecutionError(CompanyOSError):
    """A late execution result no longer owns the WorkOrder lease."""
