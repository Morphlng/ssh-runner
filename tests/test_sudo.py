"""Exercise sudo framing without SSH access or actual privilege elevation."""

import dataclasses
import io
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import run_command, run_job, transport

from ssh_runner import Config, ConfigStore, ExecutionResult, ToolError, cli
from ssh_runner.config import serialize_config
from ssh_runner.config import store as config
from ssh_runner.ssh.process import Capture


class SudoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.password = "sudo-only ' 中文 $HOME `literal` \\"
        self.config = Config(
            "example.com",
            "alice",
            22,
            "ssh-only-password",
            auth_mode="key",
            reuse_connection=False,
            sudo_password=self.password,
        )
        self.config_file = self.directory / "config.json"
        self.config_file.write_text(serialize_config(self.config))
        self.config_file.chmod(0o600)
        self.sudo = self.directory / "sudo"
        self.sudo.write_text(
            "#!%s\n" % sys.executable
            + """import os, sys
mode = os.environ.get('FAKE_SUDO_MODE', 'password')
if mode == 'password':
    if '-n' in sys.argv:
        print('sudo: a password is required', file=sys.stderr)
        sys.exit(1)
    supplied = sys.stdin.buffer.readline().rstrip(b'\\n').decode('utf-8')
    if supplied != os.environ['FAKE_SUDO_PASSWORD']:
        print('rejected ' + supplied, file=sys.stderr)
        sys.exit(1)
if mode == 'denied':
    print('sudo: command not allowed', file=sys.stderr)
    sys.exit(1)
args = sys.argv[sys.argv.index('--') + 1:]
os.execvp(args[0], args)
"""
        )
        self.sudo.chmod(0o700)
        self.ssh = self.directory / "fake-ssh"
        self.ssh.write_text(
            "#!%s\n" % sys.executable
            + """import os, sys
if '-G' in sys.argv:
    print('hostname example.com')
    sys.exit(0)
os.execv('/bin/sh', ['sh', '-c', sys.argv[-1]])
"""
        )
        self.ssh.chmod(0o700)

    def run_command(self, command, config=None, mode="password", **kwargs):
        environment = {
            "PATH": str(self.directory) + os.pathsep + os.environ["PATH"],
            "FAKE_SUDO_MODE": mode,
            "FAKE_SUDO_PASSWORD": self.password,
            "FAKE_LOGIN_PASSWORD": self.config.password,
        }
        with mock.patch.dict(os.environ, environment), mock.patch.object(
            transport, "binary", str(self.ssh)
        ):
            return run_command(config or self.config, command, timeout=5, **kwargs)

    def test_config_roundtrip_private_summaries_and_service_only_credentials(self):
        self.assertEqual(ConfigStore(self.config_file).select()[1], self.config)
        self.assertNotIn(self.password, repr(self.config))
        summary = ConfigStore(self.config_file).load().summaries()
        self.assertTrue(summary[0]["sudo_password_configured"])
        self.assertNotIn(self.password, json.dumps(summary))
        data = json.loads(self.config_file.read_text())
        data["defaults"] = {"sudo_password": self.password}
        self.config_file.write_text(json.dumps(data))
        with self.assertRaises(ToolError):
            ConfigStore(self.config_file).load()
        for value in (
            12,
            None,
            "bad\npassword",
            "bad\rpassword",
            "bad\x00password",
            "x" * 1024,
        ):
            with self.subTest(value=value), self.assertRaises(ToolError):
                dataclasses.replace(self.config, sudo_password=value)
        with self.assertRaises(ToolError):
            dataclasses.replace(self.config, env={"VALUE": self.password})

    def test_sudo_password_is_stdin_only_and_closed_after_run(self):
        captured = {}

        def run(config, command, timeout, **kwargs):
            captured.update(command=command, stdin=kwargs["stdin"])
            self.assertEqual(kwargs["stdin"].read(), self.password.encode() + b"\n")
            self.assertEqual(os.fstat(kwargs["stdin"].fileno()).st_mode & 0o777, 0o600)
            return ExecutionResult(0, False, "ok", "")

        with mock.patch.object(
            transport,
            "resolve_config",
            return_value=(self.config, "hostname example.com"),
        ), mock.patch.object(transport, "run_command", side_effect=run):
            run_command(self.config, "id", sudo=True)
        self.assertNotIn(self.password, captured["command"])
        self.assertNotIn(self.config.password, captured["command"])
        self.assertTrue(captured["stdin"].closed)

    def test_password_and_nopasswd_do_not_feed_credentials_to_target(self):
        for mode in ("password", "nopasswd"):
            with self.subTest(mode=mode):
                result = self.run_command("cat; printf done", sudo=True, mode=mode)
                self.assertEqual(result.exit_code, 0, result.stderr)
                self.assertEqual(result.stdout, "done")
        cwd = self.directory / "quoted ' directory"
        cwd.mkdir()
        result = self.run_command("pwd", sudo=True, cwd=str(cwd))
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()).resolve(), cwd.resolve())

    def test_wrong_password_and_policy_denial_never_run_target(self):
        target = self.directory / "should-not-exist"
        command = "touch " + shlex.quote(str(target))
        for config, mode in (
            (
                dataclasses.replace(self.config, sudo_password="wrong-test-password"),
                "password",
            ),
            (self.config, "denied"),
        ):
            with self.subTest(mode=mode):
                result = self.run_command(command, config=config, mode=mode, sudo=True)
                self.assertNotEqual(result.exit_code, 0)
                self.assertFalse(target.exists())
                self.assertNotIn(config.sudo_password, result.stderr)

    def test_missing_sudo_password_uses_noninteractive_policy_without_ssh_password_fallback(
        self,
    ):
        config = dataclasses.replace(self.config, sudo_password="")
        result = self.run_command(
            "printf ok", config=config, sudo=True, mode="nopasswd"
        )
        self.assertEqual((result.exit_code, result.stdout), (0, "ok"))
        result = self.run_command("printf should-not-run", config=config, sudo=True)
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "")

    def test_plain_confirmation_input_remains_supported(self):
        data = self.directory / "answer.txt"
        data.write_text("y\n")
        result = self.run_command('read answer; printf "%s" "$answer"', stdin_file=data)
        self.assertEqual((result.exit_code, result.stdout), (0, "y"))

    def test_both_credentials_are_redacted_in_results_and_full_logs(self):
        stdout = self.directory / "stdout.log"
        stderr = self.directory / "stderr.log"
        result = self.run_command(
            'printf "%s" "$FAKE_SUDO_PASSWORD"; printf "%s" "$FAKE_LOGIN_PASSWORD" >&2',
            stdout_file=stdout,
            stderr_file=stderr,
        )
        self.assertEqual(
            (result.exit_code, result.stdout, result.stderr),
            (0, "[REDACTED]", "[REDACTED]"),
        )
        self.assertEqual(stdout.read_text(), "[REDACTED]")
        self.assertEqual(stderr.read_text(), "[REDACTED]")

    def test_conflicting_input_and_embedded_credentials_fail_before_ssh(self):
        with mock.patch.object(transport, "resolve_config") as resolve:
            for command, kwargs in (
                ("id", {"sudo": True, "stdin_file": io.BytesIO(b"y\n")}),
                ("echo " + self.password, {}),
            ):
                with self.assertRaises(ToolError):
                    run_command(self.config, command, **kwargs)
            with self.assertRaises(ToolError):
                run_job(self.config, "start", command="echo " + self.password)
            resolve.assert_not_called()

    def test_cli_routes_sudo_and_rejects_unsupported_operations(self):
        output = io.StringIO()
        fake = mock.Mock()
        fake.execute.return_value = ExecutionResult(0, False, "", "")
        app = cli.Application(client_factory=lambda *a, **k: fake, stdout=output)
        self.assertEqual(
            app.main(["--config", str(self.config_file), "exec", "id", "--sudo"]), 0
        )
        self.assertTrue(fake.execute.call_args.kwargs["sudo"])
        for args in (
            ["exec", "id", "--sudo", "--stdin-file", "-"],
            ["script", "x", "--sudo"],
            ["job", "start", "x", "--sudo"],
        ):
            factory = mock.Mock()
            self.assertEqual(
                cli.Application(client_factory=factory, stdout=io.StringIO()).main(
                    args
                ),
                2,
            )
            factory.assert_not_called()

    def test_multiple_overlapping_secrets_redact_preview_and_disk_across_chunks(self):
        data = b"prefix ssh-password sudo-password password suffix"
        expected = b"prefix [REDACTED] [REDACTED] [REDACTED] suffix"
        for width in range(1, len(data) + 1):
            sink = io.BytesIO()
            capture = Capture(
                100, (b"password", b"ssh-password", b"sudo-password"), sink
            )
            for offset in range(0, len(data), width):
                capture.feed(data[offset : offset + width])
            capture.feed(b"", final=True)
            self.assertEqual(sink.getvalue(), expected)
            self.assertEqual(capture.text.encode(), expected)
        for width in range(1, 20):
            capture = Capture(100, (b"secret", b"secret-value"))
            data = b"secret-value secret!"
            for offset in range(0, len(data), width):
                capture.feed(data[offset : offset + width])
            capture.feed(b"", final=True)
            self.assertEqual(capture.text, "[REDACTED] [REDACTED]!")
        sink = io.BytesIO()
        capture = Capture(100, (b"password", b"sudo-password"), sink, binary=True)
        capture.feed(data, final=True)
        self.assertEqual(sink.getvalue(), data)
