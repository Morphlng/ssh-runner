"""Agent-oriented OpenSSH client with no third-party runtime dependencies."""

from .config import Config, ConfigCatalog, ConfigStore
from .core.errors import ToolError
from .core.results import ExecutionResult
from .services.client import SSHClient

__version__ = "0.1.0"
__all__ = [
    "Config",
    "ConfigCatalog",
    "ConfigStore",
    "ExecutionResult",
    "SSHClient",
    "ToolError",
]
