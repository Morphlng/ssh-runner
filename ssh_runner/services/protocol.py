"""Private JSON RPC over the existing exec transport, without JSON redaction damage."""

import dataclasses
import json
import shlex

from .._platform import private_temporary_file
from ..core.errors import ToolError

MAX_RESPONSE_BYTES = 1024 * 1024


def python_command(source, *arguments, interpreter="python3"):
    return (
        shlex.quote(interpreter)
        + " -c "
        + shlex.quote(source)
        + "".join(" " + shlex.quote(str(value)) for value in arguments)
    )


class RemoteProtocol:
    def __init__(self, executor):
        self.executor = executor

    def call(self, source, request, *arguments, **kwargs):
        """Return (transport result, raw parsed object); raw data must not be exposed.

        The private binary sink avoids mutating JSON escape sequences during transport
        redaction. Callers must sanitize parsed values before returning them to users.
        """
        with private_temporary_file() as payload:
            payload.write(json.dumps(request, ensure_ascii=False).encode("utf-8"))
            payload.seek(0)
            return self.receive(
                python_command(
                    source, *arguments, interpreter=self.executor.config.remote_python
                ),
                stdin_file=payload,
                **kwargs,
            )

    def receive(self, command, **kwargs):
        """Receive private JSON while allowing callers to stream arbitrary stdin."""
        run = self.executor.execute
        with private_temporary_file() as response:
            result = run(
                command,
                stdout_file=response,
                _binary_stdout=True,
                max_output_bytes=65536,
                **kwargs,
            )
            response.seek(0)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ToolError("远端结构化响应过大，未返回不完整数据。")
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            if result.exit_code != 0:
                return dataclasses.replace(result, stdout=""), None
            raise ToolError(
                "远端结构化响应无效；请检查登录横幅或远端 Python。"
            ) from None
        return dataclasses.replace(result, stdout=""), data
