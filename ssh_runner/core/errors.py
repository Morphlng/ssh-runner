"""Expected operational failures, safe to present without exposing credentials."""


class ToolError(Exception):
    def __init__(self, message, *, kind="local", job_id=None):
        super().__init__(message)
        self.kind = kind
        self.job_id = job_id


class ConfigError(ToolError):
    """Expected configuration errors, before an SSH process is created."""

    def __init__(self, message):
        super().__init__(message, kind="config")
