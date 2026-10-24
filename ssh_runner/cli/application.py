"""Parse, validate, dispatch, present: no operation-specific dispatch chain."""

import dataclasses
import signal
import sys
from functools import cached_property

from ..config import ConfigStore
from ..core.errors import ToolError
from ..core.results import ExecutionResult
from ..services.client import SSHClient
from .presentation import Presenter
from .registry import Parser, build_parser, commands


class Context:
    def __init__(self, options, presenter, registry, client_factory, store_factory):
        self.options, self.presenter, self.commands = options, presenter, registry
        self.client_factory = client_factory
        self.store = store_factory(options.config)

    @cached_property
    def catalog(self):
        return self.store.load()

    @cached_property
    def selected(self):
        return self.catalog.select(self.options.server)

    @cached_property
    def client(self):
        config = self.selected[1]
        if getattr(self.options, "no_reuse", False):
            config = dataclasses.replace(config, reuse_connection=False)
        return self.client_factory(
            config, control_dir=getattr(self.options, "control_dir", None)
        )


class _Terminated(KeyboardInterrupt):
    pass


class Application:
    def __init__(
        self,
        *,
        client_factory=SSHClient,
        store_factory=ConfigStore,
        registry=None,
        stdout=None,
        stderr=None,
    ):
        self.client_factory, self.store_factory = client_factory, store_factory
        self.commands = commands() if registry is None else registry
        self.presenter = Presenter(stdout=stdout, stderr=stderr)

    def main(self, argv=None):
        arguments = sys.argv[1:] if argv is None else argv
        options = None
        operation = None
        try:
            # Select the error presenter before full parsing. This does not rewrite argv.
            format_parser = Parser(add_help=False, allow_abbrev=False)
            format_parser.add_argument(
                "--format", choices=("json", "text"), default="json"
            )
            self.presenter.format = format_parser.parse_known_args(arguments)[0].format
            options = build_parser(self.commands).parse_args(arguments)
            command = options.handler
            operation = command.operation
            command.validate(options)
            context = Context(
                options,
                self.presenter,
                self.commands,
                self.client_factory,
                self.store_factory,
            )
            result = command.run(context, options)
            result = dataclasses.replace(result, operation=operation)
        except SystemExit as error:
            return error.code
        except (ToolError, KeyboardInterrupt) as error:
            operation = operation or getattr(error, "operation", None)
            interrupted = isinstance(error, KeyboardInterrupt)
            code = 143 if isinstance(error, _Terminated) else 130 if interrupted else 2
            identifier = getattr(error, "job_id", None)
            if (
                identifier is None
                and options is not None
                and operation
                and operation.startswith("job.")
            ):
                identifier = (
                    getattr(options, "job_id", None)
                    if operation == "job.start"
                    else getattr(options, "target", None)
                )
            result = ExecutionResult(
                code,
                False,
                "",
                "操作已中断。" if interrupted else str(error),
                job_id=identifier,
                operation=operation,
                error_kind="interrupted" if interrupted else error.kind,
            )
        self.presenter.write(
            result, event="result" if operation == "job.follow" else None
        )
        return result.exit_code


def main(argv=None):
    return Application().main(argv)


def run():
    from .._platform import WINDOWS

    if WINDOWS:
        import msvcrt
        import os

        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")
        if hasattr(sys.stdin, "fileno"):
            msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)

    def terminate(signum, frame):
        raise _Terminated()

    previous = signal.signal(signal.SIGTERM, terminate)
    previous_break = signal.signal(signal.SIGBREAK, terminate) if WINDOWS else None
    try:
        return main()
    finally:
        signal.signal(signal.SIGTERM, previous)
        if WINDOWS:
            signal.signal(signal.SIGBREAK, previous_break)
