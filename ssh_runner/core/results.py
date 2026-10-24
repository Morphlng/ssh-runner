"""Shared result contract; transport status and operation data are separate."""

import dataclasses
from typing import Any, Dict, Optional


@dataclasses.dataclass(frozen=True)
class ExecutionResult:
    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_file: Optional[str] = None
    stderr_file: Optional[str] = None
    job_id: Optional[str] = None
    operation: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    error_kind: Optional[str] = None
    schema_version: int = 1


def with_data(result, operation, data, **changes):
    """Structured operation data is never duplicated in the captured stdout."""
    changes.setdefault("stdout", "")
    return dataclasses.replace(result, operation=operation, data=data, **changes)


def redact(value, secrets):
    """Redact string values after parsing, never corrupt JSON keys or syntax."""
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    return value
