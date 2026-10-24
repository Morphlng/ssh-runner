import contextlib
import dataclasses
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import client, run_command, transport

from ssh_runner import Config, ConfigStore, ExecutionResult, ToolError, cli
from ssh_runner import config as config_module
from ssh_runner.config import store as config
from ssh_runner.ssh import auth as authentication
from ssh_runner.ssh import transport as transport_module
from ssh_runner.ssh.auth import get_policy

MODULE_PATH = Path(__file__).resolve().parents[1] / "ssh_runner" / "__main__.py"


class SSHExecTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.config_file = self.directory / "config.json"
        self.password = "test-only-$literal#secret"
        self.config_file.write_text(
            config_module.serialize_config(
                Config(
                    "example.com",
                    "staging",
                    22,
                    self.password,
                    auth_mode="keyboard-interactive",
                    reuse_connection=False,
                )
            ),
            encoding="utf-8",
        )
        self.config_file.chmod(0o600)
        self.config = ConfigStore(self.config_file).select()[1]

    def fake_ssh(self, body):
        executable = self.directory / "fake-ssh"
        inspection = "import sys\nif '-G' in sys.argv:\n    print('hostname ' + sys.argv[-1])\n    sys.exit(0)\n"
        executable.write_text(
            "#!%s\n" % sys.executable + inspection + body, encoding="utf-8"
        )
        executable.chmod(0o700)
        return str(executable)

    def test_only_expected_keyboard_interactive_challenge_is_answered(self):
        for prompt in [
            "Password:",
            "(staging@example.com) Password: ",
        ]:
            self.assertEqual(
                get_policy(self.config.auth_mode).answer(self.config, prompt),
                self.password,
            )
        for prompt in [
            "OTP:",
            "Unexpected password:",
            "Enter passphrase for key '/tmp/key':",
            "Are you sure?",
            "(other@example.com) Password:",
        ]:
            with self.subTest(prompt=prompt), self.assertRaises(ToolError):
                get_policy(self.config.auth_mode).answer(self.config, prompt)

    def test_standard_password_and_keyboard_interactive_prompts_are_separate(self):
        standard = dataclasses.replace(self.config, auth_mode="password")
        pam = dataclasses.replace(self.config, auth_mode="keyboard-interactive")
        self.assertEqual(
            get_policy(standard.auth_mode).answer(
                standard, "staging@example.com's password: "
            ),
            self.password,
        )
        for prompt in [
            "Password:",
            "OTP:",
            "other@example.com's password:",
            "Unexpected password:",
        ]:
            with self.assertRaises(ToolError):
                get_policy(standard.auth_mode).answer(standard, prompt)
        self.assertEqual(
            get_policy(pam.auth_mode).answer(pam, "Password:"), self.password
        )
        self.assertEqual(
            get_policy(pam.auth_mode).answer(pam, "(staging@example.com) Password:"),
            self.password,
        )
        with self.assertRaises(ToolError):
            get_policy(pam.auth_mode).answer(pam, "Verification code:")

    def test_password_mode_and_master_arguments_have_explicit_policy(self):
        standard = dataclasses.replace(self.config, auth_mode="password")
        direct = transport.arguments(standard, "pwd")
        self.assertIn("PasswordAuthentication=yes", direct)
        self.assertIn("KbdInteractiveAuthentication=no", direct)
        master = transport.arguments(
            standard, control_path=Path("/tmp/example.sock"), operation="master"
        )
        for option in [
            "ControlMaster=yes",
            "ControlPersist=300",
            "SessionType=none",
            "ForkAfterAuthentication=yes",
            "ServerAliveInterval=15",
            "ServerAliveCountMax=3",
        ]:
            self.assertIn(option, master)
        client = transport.arguments(
            standard, "pwd", control_path=Path("/tmp/example.sock")
        )
        for option in [
            "BatchMode=yes",
            "PasswordAuthentication=no",
            "KbdInteractiveAuthentication=no",
            "ProxyCommand=false",
            "ProxyJump=none",
        ]:
            self.assertIn(option, client)

    def test_remote_environment_does_not_modify_local_environment_or_command(self):
        selected = dataclasses.replace(
            self.config,
            env={"APP_ENV": 'space "quote" \\ $HOME # literal', "LANG": "C"},
        )
        args = transport.arguments(selected, "pwd")
        self.assertEqual(args[-1], "pwd")
        self.assertIn("SendEnv=APP_ENV LANG", args)
        self.assertEqual(sum(arg.startswith("SetEnv=") for arg in args), 1)
        with authentication.askpass_environment(selected) as environment:
            self.assertEqual(environment.get("LANG"), os.environ.get("LANG"))
            self.assertEqual(environment.get("APP_ENV"), os.environ.get("APP_ENV"))

    def test_host_alias_resolves_the_password_prompt(self):
        standard = Config("alias", "alice", 22, self.password, auth_mode="password")
        evaluated = subprocess.CompletedProcess(
            [], 0, "hostname otp-server\nhostkeyalias trusted-alias\n", ""
        )
        with mock.patch.object(transport.process, "run", return_value=evaluated):
            resolved, _ = transport.resolve_config(standard)
        self.assertEqual(resolved.password_prompt, "alice@trusted-alias's password:")
        evaluated.stdout = "hostname otp-server\n"
        with mock.patch.object(transport.process, "run", return_value=evaluated):
            resolved, _ = transport.resolve_config(standard)
        self.assertEqual(resolved.password_prompt, "alice@otp-server's password:")

    def test_check_never_starts_ssh_or_prints_password(self):
        output = io.StringIO()
        with mock.patch.object(
            transport.process, "spawn"
        ) as popen, contextlib.redirect_stdout(output):
            code = cli.main(["--config", str(self.config_file), "config", "check"])
        self.assertEqual(code, 0)
        popen.assert_not_called()
        self.assertNotIn(self.password, output.getvalue())
        self.assertEqual(
            json.loads(output.getvalue())["data"]["host"], self.config.host
        )

    def test_command_uses_strict_host_check_and_no_local_shell(self):
        command = "printf hello; pwd | head -1"
        args = transport.arguments(self.config, command)
        for option in [
            "StrictHostKeyChecking=yes",
            "UpdateHostKeys=no",
            "NumberOfPasswordPrompts=1",
            "ClearAllForwardings=yes",
            "ControlPath=none",
            "Tunnel=no",
            "PreferredAuthentications=keyboard-interactive",
            "RemoteCommand=none",
            "SessionType=default",
            "ForkAfterAuthentication=no",
        ]:
            self.assertIn(option, args)
        self.assertNotIn("-F", args)
        self.assertFalse(any("Algorithms=" in argument for argument in args))
        self.assertEqual(args[-1], command)
        self.assertNotIn(self.password, " ".join(args))

    @unittest.skipUnless(Path("/usr/bin/ssh").exists(), "OpenSSH is not installed")
    def test_openssh_effective_config_preserves_command_session_policy(self):
        ssh_config = self.directory / "ssh_config"
        ssh_config.write_text(
            "Host *\n RemoteCommand false\n SessionType none\n"
            " ForkAfterAuthentication yes\n RequestTTY force\n"
            " ControlMaster auto\n ForwardAgent yes\n Tunnel yes\n"
            " LocalForward 9999 localhost:80\n"
        )
        args = transport.arguments(self.config, "pwd")
        # -G evaluates this synthetic configuration without connecting.
        checked = subprocess.run(
            [args[0], "-G", "-F", str(ssh_config)] + args[1:],
            capture_output=True,
            text=True,
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)
        effective = dict(line.split(" ", 1) for line in checked.stdout.splitlines())
        for key, value in {
            "remotecommand": "none",
            "sessiontype": "default",
            "requesttty": "false",
            "forkafterauthentication": "no",
            "controlmaster": "false",
            "forwardagent": "no",
            "tunnel": "false",
        }.items():
            self.assertEqual(effective.get(key, "none"), value)
        self.assertNotIn("localforward", effective)

    def test_askpass_subprocess_and_output_redaction(self):
        fake = self.fake_ssh(
            """import os, pathlib, stat, subprocess, sys
assert 'SSH_PASSWORD' not in os.environ
assert 'SSHPASS' not in os.environ
assert os.environ['SSH_ASKPASS_REQUIRE'] == 'force'
assert 'StrictHostKeyChecking=yes' in sys.argv
assert 'UpdateHostKeys=no' in sys.argv
helper = os.environ['SSH_ASKPASS']
snapshot = pathlib.Path(os.environ['SSH_RUNNER_CONFIG_FILE'])
assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
assert stat.S_IMODE(snapshot.parent.stat().st_mode) == 0o700
assert stat.S_IMODE(pathlib.Path(helper).stat().st_mode) == 0o700
good = subprocess.run([helper, '(staging@example.com) Password:'], capture_output=True, text=True)
assert good.returncode == 0
secret = good.stdout.rstrip('\\n')
assert secret and secret not in ' '.join(sys.argv)
assert secret not in repr(os.environ)
bad = subprocess.run([helper, 'OTP:'], capture_output=True, text=True)
assert bad.returncode != 0 and not bad.stdout
assert secret not in bad.stderr
print('remote output: ' + secret)
print(secret, file=sys.stderr)
sys.exit(7)
"""
        )
        with mock.patch.object(transport, "binary", fake), mock.patch.dict(
            os.environ, {"SSH_PASSWORD": self.password, "SSHPASS": self.password}
        ):
            result = run_command(self.config, "pwd", 5)
        self.assertEqual(result.exit_code, 7)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.stdout, "remote output: [REDACTED]\n")
        self.assertEqual(result.stderr, "[REDACTED]\n")

    def test_snapshot_is_stable_when_source_config_changes(self):
        fake = self.fake_ssh(
            """import os, pathlib, subprocess
pathlib.Path(%r).write_text('changed-during-run')
helper = os.environ['SSH_ASKPASS']
answer = subprocess.run([helper, 'Password:'], capture_output=True, text=True)
assert answer.returncode == 0
print(answer.stdout, end='')
print(os.environ['SSH_RUNNER_CONFIG_FILE'])
"""
            % str(self.config_file)
        )
        with mock.patch.object(transport, "binary", fake):
            result = run_command(self.config, "pwd", 5)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.stdout.startswith("[REDACTED]\n"))
        self.assertNotIn("changed-during-run", result.stdout)
        self.assertFalse(Path(result.stdout.splitlines()[1]).parent.exists())

    def test_literal_quotes_unicode_and_spaces_survive_askpass(self):
        password = " 中文 'double\" $HOME `literal` # \\ "
        config = Config(
            self.config.host,
            self.config.user,
            self.config.port,
            password,
            auth_mode="keyboard-interactive",
        )
        with mock.patch.dict(os.environ, {"LC_ALL": "C", "PYTHONIOENCODING": "ascii"}):
            with authentication.askpass_environment(config) as environment:
                response = subprocess.run(
                    [environment["SSH_ASKPASS"], "Password:"],
                    env=environment,
                    capture_output=True,
                    encoding="utf-8",
                )
                snapshot = Path(environment[authentication.SNAPSHOT_ENV])
                self.assertEqual(response.returncode, 0)
                self.assertEqual(response.stdout, password + "\n")
        self.assertFalse(snapshot.parent.exists())

    def test_timeout_stops_local_process_and_cleans_snapshot(self):
        fake = self.fake_ssh(
            "import os, time\nprint(os.environ['SSH_RUNNER_CONFIG_FILE'], flush=True)\ntime.sleep(30)\n"
        )
        with mock.patch.object(transport, "binary", fake):
            result = run_command(self.config, "pwd", 1)
        self.assertEqual(result.exit_code, 124)
        self.assertTrue(result.timed_out)
        self.assertFalse(Path(result.stdout.strip()).parent.exists())

    def test_timeout_escalates_if_ssh_ignores_sigterm(self):
        fake = self.fake_ssh(
            "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('started', flush=True)\ntime.sleep(30)\n"
        )
        with mock.patch.object(transport, "binary", fake), mock.patch.object(
            transport.process, "grace", 0.1
        ):
            result = run_command(self.config, "pwd", 1)
        self.assertEqual(result.exit_code, 124)
        self.assertIn("started", result.stdout)

    def test_detached_child_cannot_hold_timeout_open_forever(self):
        pid_file = self.directory / "detached-pid"
        fake = self.fake_ssh(
            """import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
pathlib.Path(%r).write_text(str(child.pid))
print('started', flush=True)
time.sleep(30)
"""
            % str(pid_file)
        )
        try:
            with mock.patch.object(transport, "binary", fake), mock.patch.object(
                transport.process, "grace", 0.1
            ):
                started = time.monotonic()
                result = run_command(self.config, "pwd", 1)
            self.assertLess(time.monotonic() - started, 4)
            self.assertEqual(result.exit_code, 124)
            self.assertIn("started", result.stdout)
        finally:
            if pid_file.exists():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)

    def test_invalid_inputs_do_not_start_process(self):
        cases = [
            (self.password, 1),
            ("", 1),
            (" \n", 1),
            ("a\x00b", 1),
            ("pwd", 0),
            ("pwd", float("nan")),
            ("pwd", float("inf")),
        ]
        for command, timeout in cases:
            with self.subTest(command=command, timeout=timeout):
                with mock.patch.object(transport.process, "spawn") as popen:
                    with self.assertRaises(ToolError):
                        run_command(self.config, command, timeout)
                    popen.assert_not_called()

    def test_connection_failure_preserves_ssh_exit_code(self):
        fake = self.fake_ssh(
            "import sys\nprint('Host key verification failed.', file=sys.stderr)\nsys.exit(255)\n"
        )
        with mock.patch.object(transport, "binary", fake):
            result = run_command(self.config, "pwd", 5)
        self.assertEqual(result.exit_code, 255)
        self.assertIn("Host key verification failed", result.stderr)

    def test_signal_exit_code_is_normalized(self):
        fake = self.fake_ssh(
            "import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n"
        )
        with mock.patch.object(transport, "binary", fake):
            result = run_command(self.config, "pwd", 5)
        self.assertEqual(result.exit_code, 143)

    def test_launch_failure_cleans_credentials(self):
        snapshots = []

        def fail(*args, **kwargs):
            snapshots.append(Path(kwargs["env"][authentication.SNAPSHOT_ENV]))
            raise OSError("test-only failure")

        with mock.patch.object(
            transport, "resolve_config", return_value=(self.config, "settings")
        ):
            with mock.patch.object(transport.process, "spawn", side_effect=fail):
                with self.assertRaises(ToolError):
                    run_command(self.config, "pwd", 5)
        self.assertEqual(len(snapshots), 1)
        self.assertFalse(snapshots[0].parent.exists())

    def test_keyboard_interrupt_stops_process_and_cleans_credentials(self):
        fake = self.fake_ssh(
            "import os, time\nprint(os.environ['SSH_RUNNER_CONFIG_FILE'], flush=True)\ntime.sleep(30)\n"
        )
        snapshots = []
        original = transport_module.askpass_environment

        @contextlib.contextmanager
        def capture(config, prompt_host=None):
            with original(config, prompt_host) as env:
                snapshots.append(Path(env[authentication.SNAPSHOT_ENV]))
                yield env

        with mock.patch.object(transport, "binary", fake), mock.patch.object(
            transport_module, "askpass_environment", capture
        ):
            with mock.patch(
                "ssh_runner.ssh.process.Capture.feed", side_effect=KeyboardInterrupt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    run_command(self.config, "pwd", 5)
        self.assertEqual(len(snapshots), 1)
        self.assertFalse(snapshots[0].parent.exists())

    def test_json_contract_for_remote_and_local_failures(self):
        output = io.StringIO()
        expected = ExecutionResult(
            7, False, "output\n", "remote failure\n", operation="exec"
        )
        fake = mock.Mock()
        fake.execute.return_value = expected
        code = cli.Application(client_factory=lambda *a, **k: fake, stdout=output).main(
            ["--config", str(self.config_file), "exec", "pwd"]
        )
        self.assertEqual(code, 7)
        self.assertEqual(json.loads(output.getvalue()), dataclasses.asdict(expected))
        output = io.StringIO()
        code = cli.Application(stdout=output).main(
            ["--config", str(self.directory / "missing"), "exec", "pwd"]
        )
        self.assertEqual(code, 2)
        self.assertTrue(json.loads(output.getvalue())["stderr"])

    def test_cli_operations_are_mutually_exclusive(self):
        output = io.StringIO()
        self.assertEqual(
            cli.Application(stdout=output).main(["config", "check", "exec", "pwd"]), 2
        )
        self.assertEqual(json.loads(output.getvalue())["error_kind"], "argument")

    def test_module_entrypoint_works_from_source(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "ssh_runner",
                "--config",
                str(self.config_file),
                "config",
                "check",
            ],
            cwd=MODULE_PATH.parent.parent,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["data"]["host"], self.config.host)

    def test_cli_sigterm_cleans_process_and_snapshot(self):
        snapshot_file = self.directory / "snapshot-path"
        fake = self.fake_ssh(
            """import os, pathlib, time
pathlib.Path(%r).write_text(os.environ['SSH_RUNNER_CONFIG_FILE'])
time.sleep(30)
"""
            % str(snapshot_file)
        )
        self.config_file.write_text(
            config_module.serialize_config(
                dataclasses.replace(self.config, ssh_binary=fake)
            )
        )
        script = "from ssh_runner.cli import run; raise SystemExit(run())"
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                "--config",
                str(self.config_file),
                "exec",
                "pwd",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=MODULE_PATH.parent.parent,
        )
        try:
            deadline = time.monotonic() + 5
            while (
                not snapshot_file.exists()
                and time.monotonic() < deadline
                and process.poll() is None
            ):
                time.sleep(0.02)
            self.assertTrue(snapshot_file.exists())
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 143, stderr)
            self.assertEqual(json.loads(stdout)["exit_code"], 143)
            self.assertFalse(Path(snapshot_file.read_text()).parent.exists())
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


if __name__ == "__main__":
    unittest.main()
