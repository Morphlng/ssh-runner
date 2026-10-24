import contextlib
import dataclasses
import socket
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from support import manager as ConnectionManager
from support import run_command, transport

from ssh_runner import Config, ExecutionResult, ToolError
from ssh_runner.config import store as config
from ssh_runner.ssh import connection


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="st-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "control"
        self.config = Config(
            "example.com", "alice", 22, "test-only-password", auth_mode="password"
        )
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.sockets = []
        self.alive = set()
        self.starts = 0
        self.stack.enter_context(
            mock.patch.object(
                transport, "resolve_config", side_effect=lambda c: (c, "settings")
            )
        )
        self.stack.enter_context(
            mock.patch.object(transport, "control_command", side_effect=self.control)
        )
        self.start = self.stack.enter_context(
            mock.patch.object(transport, "start_master", side_effect=self.bootstrap)
        )
        self.addCleanup(self.clean_sockets)

    def clean_sockets(self):
        for sock in self.sockets:
            sock.close()

    def control(self, config, path, operation):
        if operation == "check":
            return path in self.alive
        self.alive.discard(path)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return True

    def bootstrap(self, config, path, **kwargs):
        self.starts += 1
        sock = socket.socket(socket.AF_UNIX)
        try:
            sock.bind(str(path))
        except BaseException:
            sock.close()
            raise
        path.chmod(0o600)
        self.sockets.append(sock)
        self.alive.add(path)
        return ExecutionResult(0, False, "", "")

    def manager(self, config=None):
        return ConnectionManager(config or self.config, control_dir=self.directory)

    def test_repeated_calls_reuse_one_master_and_commands_are_not_replayed(self):
        result = ExecutionResult(255, False, "partial", "command failure")
        with mock.patch.object(transport, "run_command", return_value=result) as run:
            first = run_command(self.config, "first", control_dir=self.directory)
            second = run_command(self.config, "second", control_dir=self.directory)
        self.assertEqual(self.starts, 1)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(first, dataclasses.replace(result, operation="exec"))
        self.assertEqual(second, dataclasses.replace(result, operation="exec"))
        self.assertEqual(
            [call.args[1] for call in run.call_args_list], ["first", "second"]
        )
        self.assertIsNotNone(run.call_args.args[3])

    def test_credentials_accounts_auth_modes_and_settings_isolate_connections(self):
        original = self.manager()
        variants = [
            dataclasses.replace(self.config, password="another-test-password"),
            dataclasses.replace(self.config, user="bob"),
            dataclasses.replace(self.config, auth_mode="keyboard-interactive"),
            dataclasses.replace(self.config, server_alive_interval=30),
            dataclasses.replace(self.config, env={"APP_ENV": "dev"}),
            dataclasses.replace(self.config, identity_file="/tmp/fixture-identity"),
            dataclasses.replace(self.config, certificate_file="/tmp/fixture-cert.pub"),
            dataclasses.replace(self.config, known_hosts_file="/tmp/fixture-hosts"),
            dataclasses.replace(self.config, ssh_config_file="/tmp/fixture-ssh-config"),
            dataclasses.replace(self.config, identities_only=True),
        ]
        paths = {original.control_path} | {
            self.manager(config).control_path for config in variants
        }
        self.assertEqual(len(paths), len(variants) + 1)
        self.assertNotIn(self.config.password, str(original.control_path))
        self.assertEqual((self.directory / "pool.key").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)

    def test_simultaneous_bootstrap_is_serialized(self):
        first = self.manager()
        second = self.manager()
        bootstrap = self.bootstrap

        def delayed(config, path, **kwargs):
            time.sleep(0.1)
            return bootstrap(config, path)

        self.start.side_effect = delayed
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(lambda manager: manager.ensure_connected(), [first, second])
            )
        self.assertEqual(results, [None, None])
        self.assertEqual(self.starts, 1)

    def test_stale_socket_is_replaced_before_next_command(self):
        manager = self.manager()
        manager.ensure_connected()
        self.alive.clear()
        self.assertIsNone(manager.ensure_connected())
        self.assertEqual(self.starts, 2)

    def test_failed_authentication_has_cooldown_and_never_executes_command(self):
        self.start.side_effect = None
        self.start.return_value = ExecutionResult(255, False, "", "Permission denied")
        with mock.patch.object(transport, "run_command") as run:
            failed = run_command(self.config, "pwd", control_dir=self.directory)
            with self.assertRaises(ToolError):
                run_command(self.config, "pwd", control_dir=self.directory)
        self.assertEqual(failed.exit_code, 255)
        self.assertEqual(self.start.call_count, 1)
        run.assert_not_called()
        manager = self.manager()
        with mock.patch.object(connection.time, "time", return_value=time.time() + 10):
            self.assertEqual(manager._cooldown_remaining(), 0)

    def test_bootstrap_interrupt_closes_any_already_forked_master(self):
        manager = self.manager()

        def interrupted(config, path, **kwargs):
            self.bootstrap(config, path)
            raise KeyboardInterrupt

        self.start.side_effect = interrupted
        with self.assertRaises(KeyboardInterrupt):
            manager.ensure_connected()
        self.assertFalse(manager.status()["connected"])
        self.assertTrue(manager.cooldown_path.exists())

    def test_status_and_close_do_not_create_connections(self):
        manager = self.manager()
        self.assertFalse(manager.status()["connected"])
        self.assertEqual(manager.close(), {"connected": False, "closed": False})
        self.assertEqual(self.starts, 0)
        manager.ensure_connected()
        self.assertTrue(manager.status()["connected"])
        self.assertEqual(manager.close(), {"connected": False, "closed": True})
        self.assertFalse(manager.status()["connected"])

    def test_insecure_directory_and_non_socket_path_are_rejected(self):
        self.directory.mkdir(mode=0o755)
        with self.assertRaises(ToolError):
            self.manager()
        self.directory.chmod(0o700)
        manager = self.manager()
        manager.control_path.write_text("not a socket")
        manager.control_path.chmod(0o600)
        with self.assertRaises(ToolError):
            manager.ensure_connected()
        link = self.directory.parent / "symlink"
        link.symlink_to(self.directory)
        with self.assertRaises(ToolError):
            ConnectionManager(self.config, control_dir=link)

    def test_timed_out_command_keeps_the_master_available(self):
        with mock.patch.object(
            transport, "run_command", return_value=ExecutionResult(124, True, "", "")
        ):
            result = run_command(
                self.config, "sleep 60", timeout=1, control_dir=self.directory
            )
        self.assertTrue(result.timed_out)
        self.assertTrue(self.manager().status()["connected"])
        self.assertEqual(self.starts, 1)

    def test_business_settings_do_not_split_the_ssh_pool(self):
        original = self.manager()
        for changes in (
            {"job_root": "/tmp/other jobs"},
            {"remote_python": "/opt/python"},
            {"sudo_password": "different-sudo"},
        ):
            updated = self.manager(dataclasses.replace(self.config, **changes))
            self.assertEqual(original.control_path, updated.control_path)
        with mock.patch.object(transport, "binary", "/another/ssh"):
            updated = self.manager()
            self.assertNotEqual(original.control_path, updated.control_path)


if __name__ == "__main__":
    unittest.main()
