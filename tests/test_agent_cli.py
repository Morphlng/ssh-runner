"""Public command contracts with injected clients and no SSH side effects."""

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ssh_runner import Config, ExecutionResult, SSHClient, ToolError
from ssh_runner.cli import Application
from ssh_runner.cli.registry import commands
from ssh_runner.services.execution import CommandExecutor
from ssh_runner.services.jobs import JobManager


class AgentCLITests(unittest.TestCase):
    def setUp(self):
        self.config = Config("host", "user", 22, auth_mode="key")
        self.fake = mock.Mock()
        self.factory = mock.Mock(return_value=self.fake)
        self.store = mock.Mock()
        self.store.load.return_value.select.return_value = ("dev", self.config)
        self.store_factory = mock.Mock(return_value=self.store)

    def invoke(self, args):
        output, errors = io.StringIO(), io.StringIO()
        code = Application(
            client_factory=self.factory,
            store_factory=self.store_factory,
            stdout=output,
            stderr=errors,
        ).main(args)
        self.assertEqual(errors.getvalue(), "")
        return code, json.loads(output.getvalue())

    def test_script_and_job_list_route_to_services(self):
        self.fake.script.return_value = ExecutionResult(0, False, "x", "")
        code, data = self.invoke(
            [
                "--server",
                "dev",
                "script",
                "path",
                "--arg=--apply",
                "--arg=",
                "--cwd",
                "/srv",
                "--timeout",
                "12",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(data["operation"], "script")
        self.assertEqual(self.fake.script.call_args.args, ("path",))
        self.assertEqual(self.fake.script.call_args.kwargs["args"], ["--apply", ""])
        self.assertEqual(self.fake.script.call_args.kwargs["timeout"], 12)
        self.fake.jobs.list.return_value = ExecutionResult(
            0, False, "", "", data={"job_ids": []}
        )
        self.invoke(["job", "list", "--offset", "2", "--limit", "10"])
        self.fake.jobs.list.assert_called_once_with(timeout=30, offset=2, limit=10)

    def test_invalid_arguments_fail_before_loading_configuration(self):
        cases = [
            [],
            ["unknown"],
            ["exec"],
            ["exec", "pwd", "--timeout", "bad"],
            ["exec", "pwd", "--sudo", "--stdin-file", "-"],
            ["script", "x", "--arg=x\x00y"],
            ["job", "status", "a", "--overwrite"],
            ["directory", "upload", "a", "b", "--overwrite"],
            ["exec", "pwd", "--server", "dev"],
            ["--format", "invalid", "capabilities"],
            ["skill", "install", "--scope", "project"],
        ]
        for args in cases:
            with self.subTest(args=args):
                self.store_factory.reset_mock()
                code, data = self.invoke(args)
                self.assertEqual(code, 2)
                self.assertEqual(data["error_kind"], "argument")
                self.assertEqual(data["schema_version"], 1)
                self.store_factory.assert_not_called()
                self.factory.assert_not_called()

    def test_registry_is_capabilities_source_without_configuration(self):
        code, data = self.invoke(["capabilities"])
        self.assertEqual(code, 0)
        self.assertEqual(data["data"]["version"], "0.1.0")
        self.assertEqual(
            [entry["name"] for entry in data["data"]["operations"]],
            [c.operation for c in commands()],
        )
        self.store.load.assert_not_called()
        self.factory.assert_not_called()

    def test_file_directory_doctor_and_connection_routes(self):
        cases = [
            (["file", "upload", "a", "b", "--overwrite"], self.fake.files.upload),
            (["directory", "download", "a", "b"], self.fake.directories.download),
            (["doctor"], self.fake.doctor),
        ]
        for args, function in cases:
            function.return_value = ExecutionResult(0, False, "", "", data={"ok": True})
            code, data = self.invoke(args)
            self.assertEqual(code, 0)
            self.assertEqual(data["stdout"], "")
            function.assert_called_once()
        self.fake.connection.status.return_value = {"connected": False}
        code, data = self.invoke(["connection", "status"])
        self.assertFalse(data["data"]["connected"])

    def test_follow_emits_log_events_and_exactly_one_final_result(self):
        def follow(identifier, on_event, **kwargs):
            on_event(
                ExecutionResult(
                    0,
                    False,
                    "",
                    "",
                    job_id=identifier,
                    data={"text": "中文", "next_offset": 6},
                )
            )
            return ExecutionResult(
                7,
                False,
                "",
                "",
                job_id=identifier,
                data={"state": "finished", "exit_code": 7},
            )

        self.fake.jobs.follow.side_effect = follow
        output = io.StringIO()
        code = Application(
            client_factory=self.factory, store_factory=self.store_factory, stdout=output
        ).main(["job", "follow", "a" * 32])
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(code, 7)
        self.assertEqual([e["event"] for e in events], ["logs", "result"])
        self.assertTrue(
            all(
                e["schema_version"] == 1 and e["operation"] == "job.follow"
                for e in events
            )
        )

    def test_follow_error_is_final_event_and_keeps_id(self):
        for error, code in [(ToolError("failed"), 2), (KeyboardInterrupt(), 130)]:
            self.fake.jobs.follow.side_effect = error
            actual, data = self.invoke(["job", "follow", "a" * 32])
            self.assertEqual(actual, code)
            self.assertEqual(data["event"], "result")
            self.assertEqual(data["job_id"], "a" * 32)

    def test_text_format_separates_streams_and_does_not_repeat_follow_page(self):
        self.fake.execute.return_value = ExecutionResult(7, False, "out", "err")
        out, err = io.StringIO(), io.StringIO()
        app = Application(
            client_factory=self.factory,
            store_factory=self.store_factory,
            stdout=out,
            stderr=err,
        )
        self.assertEqual(app.main(["--format", "text", "exec", "x"]), 7)
        self.assertEqual(out.getvalue(), "out")
        self.assertEqual(err.getvalue(), "err\n")

    def test_script_arguments_are_literal_and_all_services_share_executor(self):
        arguments = [
            "",
            "中文",
            "a b",
            "$(touch forbidden)",
            "x;y",
            "a\nb",
            "--flag",
            "'\"$",
        ]
        executor = mock.Mock(config=self.config)

        def run(command, **kwargs):
            completed = subprocess.run(
                command,
                shell=True,
                executable="/bin/sh",
                input=b'printf "%s\\0" "$@"',
                capture_output=True,
            )
            return ExecutionResult(
                completed.returncode,
                False,
                completed.stdout.decode(),
                completed.stderr.decode(),
            )

        executor.execute.side_effect = run
        client = SSHClient(self.config, executor=executor)
        result = client.script("unused", args=arguments)
        self.assertEqual(result.stdout.split("\x00")[:-1], arguments)
        for service in (
            client.files,
            client.directories,
            client.jobs,
            client.diagnostics,
        ):
            self.assertIs(service.executor, executor)

    def test_fifo_input_fails_before_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "input"
            os.mkfifo(fifo)
            transport = mock.Mock()
            executor = CommandExecutor(self.config, transport=transport)
            with self.assertRaises(ToolError):
                executor.execute("cat", stdin_file=fifo)
            transport.resolve_config.assert_not_called()

    def test_job_id_survives_protocol_error_and_interrupt(self):
        for error in (ToolError("bad"), KeyboardInterrupt()):
            protocol = mock.Mock()
            protocol.call.side_effect = error
            jobs = JobManager(mock.Mock(config=self.config), protocol)
            with self.assertRaises(type(error)) as caught:
                jobs.start("true", job_id="b" * 32)
            self.assertEqual(caught.exception.job_id, "b" * 32)

    def test_empty_job_id_is_not_silently_replaced(self):
        protocol = mock.Mock()
        jobs = JobManager(mock.Mock(config=self.config), protocol)
        with self.assertRaises(ToolError):
            jobs.start("true", job_id="")
        protocol.call.assert_not_called()

    def test_doctor_reports_configuration_failure_without_a_client(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            code = Application(client_factory=self.factory, stdout=output).main(
                ["--config", str(Path(directory) / "missing"), "doctor"]
            )
        data = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(data["data"]["checks"][0]["stage"], "config")
        self.factory.assert_not_called()
