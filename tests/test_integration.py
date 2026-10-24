"""Optional real OpenSSH tests against a disposable loopback SSH server."""

import contextlib
import dataclasses
import json
import logging
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from support import client
from support import manager as ConnectionManager
from support import run_command, transport

from ssh_runner import Config, ConfigStore, ToolError
from ssh_runner import config as config_module
from ssh_runner.config import store as config
from ssh_runner.ssh import transport as transport_module

try:
    import paramiko
except ImportError:
    paramiko = None


if paramiko is not None:

    class TestServer(paramiko.ServerInterface):
        def __init__(self, owner):
            self.owner = owner

        def get_allowed_auths(self, username):
            return {"password": "password", "key": "publickey"}.get(
                self.owner.mode, "keyboard-interactive"
            )

        def check_auth_publickey(self, username, key):
            self.owner.auth_attempts += 1
            return (
                paramiko.AUTH_SUCCESSFUL
                if username == "alice" and key == self.owner.key
                else paramiko.AUTH_FAILED
            )

        def check_auth_password(self, username, password):
            self.owner.auth_attempts += 1
            return (
                paramiko.AUTH_SUCCESSFUL
                if username == "alice" and password == self.owner.password
                else paramiko.AUTH_FAILED
            )

        def check_auth_interactive(self, username, submethods):
            if getattr(self.owner, "challenges", None) is not None:
                return paramiko.InteractiveQuery(
                    "", "", *((label, False) for label in self.owner.challenges)
                )
            challenge = self.owner.challenge
            return paramiko.InteractiveQuery("", "", (challenge, False))

        def check_auth_interactive_response(self, responses):
            self.owner.auth_attempts += 1
            self.owner.responses.append(responses)
            return (
                paramiko.AUTH_SUCCESSFUL
                if responses == [self.owner.password]
                else paramiko.AUTH_FAILED
            )

        def check_channel_request(self, kind, chanid):
            return (
                paramiko.OPEN_SUCCEEDED
                if kind == "session"
                else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
            )

        def check_channel_env_request(self, channel, name, value):
            name, value = name.decode("utf-8"), value.decode("utf-8")
            if self.owner.accept_env is not None and name not in self.owner.accept_env:
                return False
            if not hasattr(channel, "remote_env"):
                channel.remote_env = {}
            channel.remote_env[name] = value
            return True

        def check_channel_exec_request(self, channel, command):
            command = command.decode("utf-8")
            self.owner.commands.append(command)

            def finish():
                # Let Paramiko send the exec-request acknowledgement first.
                self.owner.stop.wait(0.03)
                if command == "stdin-eof-test":
                    import hashlib

                    incoming = channel.makefile("rb").read()
                    channel.sendall(
                        (hashlib.sha256(incoming).hexdigest() + "\n").encode("ascii")
                    )
                    channel.send_exit_status(0)
                    channel.close()
                    return
                if getattr(self.owner, "execute_shell", False):
                    incoming = channel.makefile("rb").read()
                    if getattr(self.owner, "corrupt_stdin", False):
                        incoming = incoming[:-1]
                    environment = os.environ.copy()
                    environment.update(HOME=str(self.owner.directory))
                    environment.update(getattr(channel, "remote_env", {}))
                    completed = subprocess.run(
                        command,
                        shell=True,
                        executable="/bin/sh",
                        input=incoming,
                        capture_output=True,
                        cwd=self.owner.directory,
                        env=environment,
                        timeout=15,
                    )
                    try:
                        channel.sendall(
                            getattr(self.owner, "stdout_prefix", b"") + completed.stdout
                        )
                        channel.sendall_stderr(completed.stderr)
                        channel.send_exit_status(completed.returncode)
                        channel.close()
                    except (EOFError, OSError):
                        pass
                    return
                if command == "sleep-test":
                    self.owner.stop.wait(5)
                try:
                    output = (
                        self.owner.password if command == "echo-secret" else command
                    )
                    if command == "env-test":
                        output = json.dumps(
                            getattr(channel, "remote_env", {}), ensure_ascii=False
                        )
                    channel.sendall((output + "\n").encode("utf-8"))
                    channel.send_exit_status(0)
                    channel.close()
                except (EOFError, OSError):
                    pass

            threading.Thread(target=finish, daemon=True).start()
            return True

        def check_global_request(self, kind, msg):
            if kind == "keepalive@openssh.com":
                self.owner.keepalives += 1
            return True


@unittest.skipUnless(
    paramiko is not None and Path("/usr/bin/ssh").exists(),
    "optional Paramiko/OpenSSH dependencies unavailable",
)
class OpenSSHIntegrationTests(unittest.TestCase):
    def setUp(self):
        logging.getLogger("paramiko").setLevel(logging.CRITICAL)
        self.temp = tempfile.TemporaryDirectory(prefix="si-", dir="/tmp")
        self.directory = Path(self.temp.name)
        self.control_dir = self.directory / "control"
        self.password = "test-only-local-password"
        self.mode = "password"
        self.challenge = "Password:"
        self.auth_attempts = 0
        self.network_connections = 0
        self.keepalives = 0
        self.commands = []
        self.responses = []
        self.accept_env = None
        self.transports = []
        self.managers = []
        self.stop = threading.Event()
        self.key = paramiko.RSAKey.generate(2048)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(5)
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.known_hosts = self.directory / "known_hosts"
        self.known_hosts.write_text(
            "[127.0.0.1]:%d %s %s\n"
            % (self.port, self.key.get_name(), self.key.get_base64())
        )
        ssh_config = self.directory / "ssh_config"
        ssh_config.write_text(
            "Host local-alias\n HostName 127.0.0.1\n"
            "Host *\n UserKnownHostsFile %s\n GlobalKnownHostsFile /dev/null\n"
            % self.known_hosts
        )
        wrapper = self.directory / "ssh"
        wrapper.write_text(
            '#!/bin/sh\nexec /usr/bin/ssh -F %s "$@"\n' % shlex.quote(str(ssh_config))
        )
        wrapper.chmod(0o700)
        self.wrapper = wrapper
        self.patch = mock.patch.object(transport, "binary", str(wrapper))
        self.patch.start()
        self.config = Config(
            "local-alias",
            "alice",
            self.port,
            self.password,
            auth_mode="password",
            ssh_binary=str(self.wrapper),
            control_persist=5,
            server_alive_interval=1,
            server_alive_count_max=2,
            connect_timeout=5,
            reconnect_delay=1,
        )
        self.acceptor = threading.Thread(target=self.accept, daemon=True)
        self.acceptor.start()

    def accept(self):
        while not self.stop.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.network_connections += 1

            def serve(client=client):
                session = paramiko.Transport(client)
                self.transports.append(session)
                session.add_server_key(self.key)
                try:
                    session.start_server(server=TestServer(self))
                    while session.is_active() and not self.stop.wait(0.05):
                        pass
                except (EOFError, OSError, paramiko.SSHException):
                    pass
                finally:
                    session.close()

            threading.Thread(target=serve, daemon=True).start()

    def tearDown(self):
        try:
            for manager in self.managers:
                with contextlib.suppress(ToolError, OSError):
                    manager.close()
        finally:
            self.stop.set()
            self.listener.close()
            for session in self.transports:
                session.close()
            self.acceptor.join(timeout=1)
            self.patch.stop()
            self.temp.cleanup()

    def manager(self, config=None):
        manager = ConnectionManager(config or self.config, control_dir=self.control_dir)
        self.managers.append(manager)
        return manager

    def run_command(self, command, config=None, timeout=3):
        selected = config or self.config
        self.manager(
            selected
        )  # Register the connection for cleanup on assertion failures.
        return run_command(selected, command, timeout, control_dir=self.control_dir)

    def wait_until(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(predicate())

    def test_custom_prompt_with_trailing_space(self):
        self.mode = "keyboard-interactive"
        self.challenge = "Enter password: "
        selected = dataclasses.replace(
            self.config, auth_mode=self.mode, password_prompt=self.challenge
        )
        self.assertEqual(self.run_command("probe", selected).exit_code, 0)

    def test_publickey_authentication_short_and_reused(self):
        self.mode = "key"
        key_file = self.directory / "identity"
        self.key.write_private_key_file(str(key_file))
        key_file.chmod(0o600)
        with mock.patch.object(
            transport_module,
            "askpass_environment",
            side_effect=AssertionError(
                "Key authentication must not create a password snapshot"
            ),
        ):
            for reuse in (False, True, True):
                result = self.run_command(
                    "key-probe",
                    dataclasses.replace(
                        self.config,
                        auth_mode="key",
                        password="",
                        reuse_connection=reuse,
                        identity_file=str(key_file),
                        known_hosts_file=str(self.known_hosts),
                    ),
                )
                self.assertEqual(result.exit_code, 0, result.stderr)
                self.assertEqual(result.stdout, "key-probe\n")
        self.assertEqual(self.network_connections, 2)

    def test_custom_ssh_config_alias_inherits_target_and_key_short_and_reused(self):
        self.mode = "key"
        key_file = self.directory / "identity"
        self.key.write_private_key_file(str(key_file))
        key_file.chmod(0o600)
        custom = self.directory / ".ssh_config"
        custom.write_text(
            "Host aliyun\n HostName 127.0.0.1\n User alice\n Port %d\n"
            " IdentityFile %s\n IdentitiesOnly yes\n UserKnownHostsFile %s\n"
            " GlobalKnownHostsFile /dev/null\n"
            % (self.port, key_file, self.known_hosts)
        )
        selected = dataclasses.replace(
            self.config,
            host="aliyun",
            user=None,
            port=None,
            password="",
            auth_mode="key",
            ssh_config_file=str(custom),
        )
        # Bypass the fixture's wrapper so the real -F option is exercised.
        with mock.patch.object(transport, "binary", "/usr/bin/ssh"):
            for reuse in (False, True, True):
                response = self.run_command(
                    "alias-key-probe",
                    dataclasses.replace(selected, reuse_connection=reuse),
                )
                self.assertEqual(response.exit_code, 0, response.stderr)
                self.assertEqual(response.stdout, "alias-key-probe\n")
        self.assertEqual(self.network_connections, 2)

    def test_wrong_explicit_key_never_retries_or_executes_command(self):
        self.mode = "key"
        wrong_key = self.directory / "wrong-key"
        paramiko.RSAKey.generate(2048).write_private_key_file(str(wrong_key))
        wrong_key.chmod(0o600)
        selected = dataclasses.replace(
            self.config,
            auth_mode="key",
            password="",
            identity_file=str(wrong_key),
            reconnect_delay=5,
        )
        failed = self.run_command("must-not-run", selected)
        self.assertEqual(failed.exit_code, 255)
        with self.assertRaises(ToolError):
            self.run_command("must-not-run", selected)
        self.assertEqual(self.network_connections, 1)
        self.assertEqual(self.auth_attempts, 1)
        self.assertEqual(self.commands, [])

    def test_real_password_login_and_master_reuse(self):
        first = self.run_command("first")
        second = self.run_command("second")
        self.assertEqual(first.exit_code, 0, first.stderr)
        self.assertEqual(second.exit_code, 0, second.stderr)
        self.assertEqual((first.stdout, second.stdout), ("first\n", "second\n"))
        self.assertEqual(self.auth_attempts, 1)
        self.assertEqual(self.network_connections, 1)
        self.assertEqual(self.commands, ["first", "second"])

    def test_short_connections_support_password_modes_with_aliases(self):
        for mode in ("password", "keyboard-interactive"):
            self.mode = mode
            selected = dataclasses.replace(
                self.config, auth_mode=mode, reuse_connection=False
            )
            for command in (mode + "-one", mode + "-two"):
                result = self.run_command(command, selected)
                self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(self.auth_attempts, 4)
        self.assertEqual(self.network_connections, 4)

    def test_dead_connection_is_rebuilt_on_the_next_explicit_call(self):
        first = self.run_command("first")
        self.assertEqual(first.exit_code, 0, first.stderr)
        for session in self.transports:
            session.close()
        manager = self.manager()
        self.wait_until(lambda: not manager.status()["connected"])
        second = self.run_command("second")
        self.assertEqual(second.exit_code, 0, second.stderr)
        self.assertEqual(self.auth_attempts, 2)
        self.assertEqual(self.commands, ["first", "second"])

    def test_changed_password_cannot_reuse_old_credentials_and_failures_cool_down(self):
        first = self.run_command("first")
        self.assertEqual(first.exit_code, 0, first.stderr)
        changed = dataclasses.replace(
            self.config, password="wrong-test-password", reconnect_delay=5
        )
        failed = self.run_command("must-not-run", changed)
        self.assertEqual(failed.exit_code, 255)
        with self.assertRaises(ToolError):
            self.run_command("must-not-run", changed)
        self.assertEqual(self.auth_attempts, 2)
        self.assertEqual(self.commands, ["first"])

    def test_keepalive_and_explicit_close(self):
        result = self.run_command("first")
        self.assertEqual(result.exit_code, 0, result.stderr)
        manager = self.manager()
        self.wait_until(lambda: self.keepalives >= 1)
        self.assertTrue(manager.status()["connected"])
        self.assertTrue(manager.close()["closed"])
        self.wait_until(lambda: not manager.status()["connected"])

    def test_idle_master_expires_and_next_call_reconnects(self):
        selected = dataclasses.replace(self.config, control_persist=1)
        first = self.run_command("first", selected)
        self.assertEqual(first.exit_code, 0, first.stderr)
        manager = self.manager(selected)
        self.wait_until(lambda: not manager.status()["connected"])
        second = self.run_command("second", selected)
        self.assertEqual(second.exit_code, 0, second.stderr)
        self.assertEqual(self.auth_attempts, 2)
        self.assertEqual(self.commands, ["first", "second"])

    def test_command_timeout_preserves_master_and_does_not_repeat_command(self):
        result = self.run_command("sleep-test", timeout=0.2)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.exit_code, 124)
        self.assertTrue(self.manager().status()["connected"])
        next_result = self.run_command("after-timeout")
        self.assertEqual(next_result.exit_code, 0, next_result.stderr)
        self.assertEqual(self.auth_attempts, 1)
        self.assertEqual(self.commands, ["sleep-test", "after-timeout"])

    def test_missing_socket_never_falls_back_to_network_authentication(self):
        result = transport.run_command(
            self.config, "must-not-run", 2, self.control_dir / "missing.sock"
        )
        self.assertEqual(result.exit_code, 255)
        self.assertEqual(self.network_connections, 0)
        self.assertEqual(self.commands, [])

    def test_keyboard_interactive_with_host_aliases(self):
        for mode in ("keyboard-interactive",):
            self.mode = mode
            selected = dataclasses.replace(self.config, auth_mode=mode)
            result = self.run_command("mode-" + mode, selected)
            self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(self.auth_attempts, 1)

    def test_otp_is_rejected_and_password_echo_is_redacted(self):
        redacted = self.run_command("echo-secret")
        self.assertEqual(redacted.stdout, "[REDACTED]\n")
        self.mode = "keyboard-interactive"
        self.challenge = "OTP:"
        selected = dataclasses.replace(self.config, auth_mode="keyboard-interactive")
        rejected = self.run_command("must-not-run", selected)
        self.assertEqual(rejected.exit_code, 255)
        self.assertNotIn(self.password, rejected.stderr)
        self.assertEqual(self.responses, [[""]])
        self.assertEqual(self.commands, ["echo-secret"])

    def test_two_separate_cli_processes_share_one_authenticated_connection(self):
        config_file = self.directory / "config.json"
        data = json.loads(config_module.serialize_config(self.config))
        data["servers"]["dev"] = data["servers"].pop("snapshot")
        data["default_server"] = "dev"
        config_file.write_text(json.dumps(data))
        config_file.chmod(0o600)
        self.manager()
        code = "from ssh_runner.cli import run; raise SystemExit(run())"
        for command in ("cli-one", "cli-two"):
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    code,
                    "--config",
                    str(config_file),
                    "--server",
                    "dev",
                    "exec",
                    command,
                    "--control-dir",
                    str(self.control_dir),
                ],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=Path(__file__).resolve().parents[1],
            )
            self.assertEqual(
                completed.returncode, 0, completed.stderr + completed.stdout
            )
            self.assertEqual(json.loads(completed.stdout)["stdout"], command + "\n")
        self.assertEqual(self.auth_attempts, 1)
        self.assertEqual(self.network_connections, 1)

    def test_remote_environment_survives_quoting_short_and_reused_connections(self):
        values = {
            "APP_LABEL": ' 中文 space "quote" \\ # $HOME `literal` ',
            "EMPTY": "",
            "PATH": "/remote/bin",
        }
        selected = dataclasses.replace(self.config, env=values, send_env=["REGION"])
        for reuse in (False, True, True):
            with mock.patch.dict(os.environ, {"REGION": "cn-test"}):
                result = self.run_command(
                    "env-test", dataclasses.replace(selected, reuse_connection=reuse)
                )
            self.assertEqual(result.exit_code, 0, result.stderr)
            received = json.loads(result.stdout)
            for name, value in {**values, "REGION": "cn-test"}.items():
                self.assertEqual(received[name], value)
        self.assertEqual(self.auth_attempts, 2)

    def test_named_targets_and_environment_values_isolate_sessions(self):
        config_file = self.directory / "config.json"
        values = json.loads(config_module.serialize_config(self.config))["servers"][
            "snapshot"
        ]
        config_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "servers": {
                        "dev": {**values, "env": {"APP_ENV": "dev"}},
                        "prod": {**values, "env": {"APP_ENV": "prod"}},
                    },
                }
            )
        )
        config_file.chmod(0o600)
        for name in ("dev", "prod", "dev"):
            selected = ConfigStore(config_file).select(name)[1]
            result = self.run_command("env-test", selected)
            self.assertEqual(result.exit_code, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["APP_ENV"], name)
        self.assertEqual(self.auth_attempts, 2)

    def test_server_can_decline_environment_variables(self):
        self.accept_env = {"APP_ENV"}
        selected = dataclasses.replace(
            self.config, env={"APP_ENV": "dev", "IGNORED": "value"}
        )
        result = self.run_command("env-test", selected)
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"APP_ENV": "dev"})


if __name__ == "__main__":
    unittest.main()
