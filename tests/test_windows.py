"""Native Windows regressions. SSH fixtures use only disposable loopback peers."""

import contextlib
import ctypes
import dataclasses
import hashlib
import io
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import test_integration as integration

from ssh_runner import Config, ConfigStore, ExecutionResult, SSHClient, ToolError
from ssh_runner._platform import (
    WINDOWS,
    archive_options,
    directory_link,
    open_private,
    open_regular,
    private_directory,
    private_temporary_directory,
    publish_file,
)
from ssh_runner._platform.archive import windows_member_key
from ssh_runner.config.store import serialize_config
from ssh_runner.core.defaults import default_config_path
from ssh_runner.remote import archive
from ssh_runner.services.execution import CommandExecutor
from ssh_runner.ssh.auth import SNAPSHOT_ENV, askpass_environment
from ssh_runner.ssh.process import Capture, ProcessRunner
from ssh_runner.ssh.state import ConnectionState, DirectConnection, _locked


class WindowsArchiveNameTests(unittest.TestCase):
    def test_names_rejected_before_extraction(self):
        for name in (
            "C:/escape",
            "C:escape",
            "//server/share",
            "a\\..\\escape",
            "a:stream",
            "NUL.txt",
            "NUL .txt",
            "CONIN$",
            "com1",
            "a/LPT².txt",
            "a.",
            "a ",
            "../escape",
            "/escape",
            "a\x00b",
            'a"b',
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                windows_member_key(name)
        self.assertEqual(windows_member_key("中文/normal.txt"), "中文/normal.txt")

    def test_implicit_directory_aliases_never_publish(self):
        with tempfile.TemporaryDirectory() as directory, io.BytesIO() as stream:
            with tarfile.open(fileobj=stream, mode="w") as writer:
                for name in ("A/file", "a/other"):
                    writer.addfile(tarfile.TarInfo(name), io.BytesIO())
            stream.seek(0)
            publish = mock.Mock()
            with self.assertRaises(ValueError):
                archive.unpack_directory(
                    stream,
                    Path(directory) / "out",
                    member_key=windows_member_key,
                    publish=publish,
                    windows=True,
                )
            publish.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])


@unittest.skipUnless(WINDOWS, "requires native Windows APIs")
class WindowsPlatformTests(unittest.TestCase):
    def setUp(self):
        self.context = private_temporary_directory(prefix="windows-tests-")
        self.root = Path(self.context.__enter__())
        self.addCleanup(self.context.__exit__, None, None, None)

    def write_private(self, path, data=b"fixture"):
        with os.fdopen(
            open_private(path, os.O_RDWR | os.O_CREAT | os.O_EXCL), "wb"
        ) as handle:
            handle.write(data)

    def test_private_config_initialization_and_platform_default(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}):
            self.assertEqual(
                default_config_path(), self.root / "ssh-runner/config.json"
            )
            store = ConfigStore()
            store.initialize()
            self.assertEqual(len(store.load().servers), 2)
            before = store.path.read_bytes()
            with self.assertRaises(ToolError):
                store.initialize()
            self.assertEqual(store.path.read_bytes(), before)
        self.assertFalse(Config("fixture", auth_mode="key").reuse_connection)

    def test_broad_acl_and_foreign_owner_are_rejected(self):
        from ssh_runner._platform import windows as win

        path = self.root / "config.json"
        self.write_private(path)
        with mock.patch.object(win, "sid_string", return_value="S-1-5-18"):
            with self.assertRaises(PermissionError):
                open_private(path)
        set_security = win.bind(
            win.security,
            "SetFileSecurityW",
            [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_void_p],
        )
        descriptor = ctypes.c_void_p()
        win.checked(
            win.StringToDescriptor("D:P(A;;FA;;;WD)", 1, ctypes.byref(descriptor), None)
        )
        try:
            win.checked(set_security(str(path), 0x80000004, descriptor))
        finally:
            win.LocalFree(descriptor)
        with self.assertRaises(PermissionError):
            open_private(path)

    def test_junctions_are_links_but_never_private_files_or_archive_sources(self):
        source = self.root / "source"
        source.mkdir()
        (source / "keep").write_bytes(b"unchanged")
        link = self.root / "junction"
        directory_link(link, source)
        self.assertEqual(link.resolve(), source.resolve())
        with self.assertRaises(OSError):
            private_directory(link)
        with self.assertRaises(OSError):
            open_regular(link)
        with io.BytesIO() as stream, self.assertRaises(ValueError):
            archive.pack_directory(link, stream, **archive_options("pack"))
        link.rmdir()
        self.assertEqual((source / "keep").read_bytes(), b"unchanged")

    def test_atomic_file_and_directory_publication_never_clobbers(self):
        source, target = self.root / "stage", self.root / "target"
        source.write_bytes(b"new")
        target.write_bytes(b"old")
        with self.assertRaises(FileExistsError):
            publish_file(source, target)
        self.assertEqual(target.read_bytes(), b"old")
        publish_file(source, target, overwrite=True)
        self.assertEqual(target.read_bytes(), b"new")
        source.mkdir()
        target.unlink()
        target.mkdir()
        with self.assertRaises(FileExistsError):
            os.rename(source, target)
        self.assertTrue(source.is_dir())

    def test_extraction_rejects_windows_names_and_roundtrips_binary(self):
        for name in ("a\\escape", "CON", "a:ads", "A.", "C:escape"):
            with self.subTest(name=name), io.BytesIO() as stream:
                with tarfile.open(fileobj=stream, mode="w") as writer:
                    writer.addfile(tarfile.TarInfo(name))
                stream.seek(0)
                with self.assertRaises(ValueError):
                    archive.unpack_directory(
                        stream, self.root / "out", **archive_options("unpack")
                    )
                self.assertFalse((self.root / "out").exists())
        source = self.root / "source"
        source.mkdir()
        data = bytes(range(256)) * 100
        (source / "中文 'file").write_bytes(data)
        with io.BytesIO() as stream:
            archive.pack_directory(source, stream, **archive_options("pack"))
            archive.unpack_directory(
                stream, self.root / "out", **archive_options("unpack")
            )
        self.assertEqual((self.root / "out" / "中文 'file").read_bytes(), data)

    def test_askpass_literal_credentials_snapshot_and_cleanup(self):
        secret = " 中文 'double\" $HOME `literal` # \\ "
        config = Config("fixture", "user", 22, secret, auth_mode="keyboard-interactive")
        with askpass_environment(config) as env:
            snapshot = Path(env[SNAPSHOT_ENV])
            fd = open_private(snapshot)
            os.close(fd)
            self.assertNotIn(secret, repr(env))
            for prompt, expected in (
                ("Password:", 0),
                ("OTP:", 1),
                ("Enter passphrase:", 1),
                ("New password:", 1),
            ):
                response = subprocess.run(
                    [env["SSH_ASKPASS"], prompt],
                    env=env,
                    capture_output=True,
                    encoding="utf-8",
                    timeout=5,
                )
                self.assertEqual(response.returncode, expected, response.stderr)
                self.assertEqual(response.stdout, secret + "\n" if not expected else "")
        self.assertFalse(snapshot.parent.exists())

    def test_status_and_close_do_not_launch_ssh_and_explicit_reuse_fails(self):
        transport = mock.Mock()
        client = SSHClient(Config("fixture", auth_mode="key"), transport=transport)
        self.assertEqual(client.connection.status()["mode"], "direct")
        self.assertFalse(client.connection.close()["closed"])
        transport.resolve_config.assert_not_called()
        config = Config("fixture", auth_mode="key", reuse_connection=True)
        with self.assertRaises(ToolError):
            SSHClient(config, transport=transport).execute("pwd")
        transport.run_command.assert_not_called()

    def test_doctor_identifies_local_storage_failure_without_connecting(self):
        config = Config("fixture", auth_mode="key")
        transport = mock.Mock()
        client = SSHClient(config, transport=transport)
        with mock.patch(
            "ssh_runner.services.diagnostics.private_temporary_file",
            side_effect=PermissionError("fixture ACL failure"),
        ):
            result = client.doctor()
        self.assertEqual(result.error_kind, "private_storage")
        self.assertEqual(result.data["checks"][-1]["stage"], "private_storage")
        transport.resolve_config.assert_not_called()
        transport.run_command.assert_not_called()

    def test_cooldown_is_shared_across_processes_and_does_not_replay(self):
        config = Config("fixture", "user", 22, "test-password", auth_mode="password")
        transport = mock.Mock(binary="fixture-ssh")
        transport.resolve_config.side_effect = lambda c: (c, "settings")
        transport.run_command.return_value = ExecutionResult(
            255, False, "partial", "failure"
        )
        manager = DirectConnection(config, transport, self.root)
        self.assertEqual(manager.execute("once", 3).exit_code, 255)
        with self.assertRaises(ToolError):
            manager.execute("never", 3)
        transport.run_command.assert_called_once()
        state = ConnectionState(config, transport, self.root)
        changed = ConnectionState(
            dataclasses.replace(config, password="different"), transport, self.root
        )
        self.assertNotEqual(state.cooldown_path, changed.cooldown_path)
        code = """import pathlib,sys
from unittest import mock
from ssh_runner import Config,ToolError
from ssh_runner.ssh.state import ConnectionState,_locked
t=mock.Mock(binary='fixture-ssh'); t.resolve_config.side_effect=lambda c:(c,'settings')
s=ConnectionState(Config('fixture','user',22,'test-password',auth_mode='password'),t,pathlib.Path(sys.argv[1]))
try:
 with _locked(s.lock_path,2): s.check_cooldown()
except ToolError: sys.exit(17)
sys.exit(0)
"""
        response = subprocess.run(
            [sys.executable, "-c", code, str(self.root)], capture_output=True, timeout=5
        )
        self.assertEqual(response.returncode, 17, response.stderr)

    def test_direct_connections_run_concurrently(self):
        transport = mock.Mock(binary="fixture-ssh")
        transport.resolve_config.side_effect = lambda c: (c, "settings")
        barrier = threading.Barrier(2, timeout=4)

        def execute(*args, **kwargs):
            barrier.wait()
            return ExecutionResult(0, False, "ok", "")

        transport.run_command.side_effect = execute
        config = Config("fixture", auth_mode="key")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: DirectConnection(config, transport, self.root).execute(
                        "pwd", 3
                    ),
                    range(2),
                )
            )
        self.assertEqual([r.exit_code for r in results], [0, 0])

    def test_concurrent_success_cannot_clear_a_recorded_failure(self):
        transport = mock.Mock(binary="fixture-ssh")
        transport.resolve_config.side_effect = lambda c: (c, "settings")
        both_started = threading.Barrier(2, timeout=3)
        failed = threading.Event()

        def execute(config, command, *args, **kwargs):
            both_started.wait()
            if command == "failure":
                return ExecutionResult(255, False, "", "failed")
            self.assertTrue(failed.wait(3))
            return ExecutionResult(0, False, "ok", "")

        transport.run_command.side_effect = execute
        config = Config("fixture", auth_mode="key")
        manager = DirectConnection(config, transport, self.root)

        def call(command):
            result = manager.execute(command, 3)
            if command == "failure":
                failed.set()
            return result

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(call, ["failure", "success"]))
        self.assertEqual([r.exit_code for r in results], [255, 0])
        with self.assertRaises(ToolError):
            manager.execute("never", 3)
        self.assertEqual(transport.run_command.call_count, 2)

    def test_parent_exit_closes_job_and_disk_errors_stop_readers(self):
        from ssh_runner._platform.windows import CloseHandle, bind, kernel
        from ssh_runner._platform.windows_process import OpenProcess

        wait = bind(
            kernel,
            "WaitForSingleObject",
            [ctypes.c_void_p, ctypes.c_ulong],
            ctypes.c_ulong,
        )
        code = """import os,sys,subprocess
from ssh_runner.ssh.process import ProcessRunner
p=ProcessRunner().spawn([sys.executable,'-c','import time; time.sleep(30)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
print(p.pid,flush=True)
os._exit(0)
"""
        parent = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=5
        )
        self.assertEqual(parent.returncode, 0, parent.stderr)
        handle = OpenProcess(0x00100000, False, int(parent.stdout.strip()))
        if handle:
            try:
                self.assertEqual(wait(handle, 2000), 0)
            finally:
                CloseHandle(handle)
        runner = ProcessRunner(grace=0.5)
        child = runner.spawn(
            [
                sys.executable,
                "-c",
                "import os,time; os.write(1,b'data'); time.sleep(30)",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        sink = mock.Mock()
        sink.write.side_effect = OSError("fixture disk failure")
        with self.assertRaises(OSError):
            runner.drain(child, Capture(100, file=sink), Capture(100), 3)
        self.assertIsNotNone(child.poll())
        self.assertFalse(
            any(t.name == "ssh-runner-pipe" for t in threading.enumerate())
        )

    def test_process_dual_stream_binary_and_redaction(self):
        runner = ProcessRunner(grace=0.5)
        process = runner.spawn(
            [
                sys.executable,
                "-c",
                "import os; os.write(1,b'x'*200000+b'sec'); os.write(2,b'y'*200000); os.write(1,b'ret')",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        sink = io.BytesIO()
        out, err = Capture(10, b"secret", sink), Capture(10)
        self.assertFalse(runner.drain(process, out, err, 5))
        self.assertEqual(sink.getvalue(), b"x" * 200000 + b"[REDACTED]")
        self.assertTrue(out.truncated and err.truncated)
        self.assertEqual((out.total, err.total), (200006, 200000))
        process = runner.spawn(
            [sys.executable, "-c", "import os; os.write(1,bytes(range(256)))"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        sink = io.BytesIO()
        self.assertFalse(
            runner.drain(process, Capture(0, file=sink, binary=True), Capture(100), 5)
        )
        self.assertEqual(sink.getvalue(), bytes(range(256)))

    def test_timeout_and_interrupt_kill_descendants_and_release_readers(self):
        from ssh_runner._platform.windows import CloseHandle
        from ssh_runner._platform.windows_process import OpenProcess

        for interrupt in (False, True):
            with self.subTest(interrupt=interrupt):
                runner = ProcessRunner(grace=0.5)
                code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); print(p.pid,flush=True); time.sleep(30)"
                process = runner.spawn(
                    [sys.executable, "-c", code],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                out = Capture(100)
                feed = out.feed

                def interrupted(data, final=False):
                    feed(data, final)
                    if data:
                        raise KeyboardInterrupt()

                start = time.monotonic()
                if interrupt:
                    out.feed = interrupted
                    with self.assertRaises(KeyboardInterrupt):
                        runner.drain(process, out, Capture(100), 4)
                else:
                    self.assertTrue(runner.drain(process, out, Capture(100), 0.5))
                self.assertLess(time.monotonic() - start, 4)
                handle = OpenProcess(0x00100000, False, int(out.text.strip()))
                if handle:
                    # A terminated process may still have a kernel object.
                    from ssh_runner._platform.windows import bind, kernel

                    wait = bind(
                        kernel,
                        "WaitForSingleObject",
                        [ctypes.c_void_p, ctypes.c_ulong],
                        ctypes.c_ulong,
                    )
                    self.assertEqual(wait(handle, 2000), 0)
                    CloseHandle(handle)
                self.assertFalse(
                    any(t.name == "ssh-runner-pipe" for t in threading.enumerate())
                )


@unittest.skipUnless(
    WINDOWS and integration.paramiko is not None and shutil.which("ssh"),
    "requires Windows OpenSSH and optional Paramiko",
)
class WindowsSSHIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.context = private_temporary_directory(prefix="windows-ssh-")
        self.directory = Path(self.context.__enter__())
        self.addCleanup(self.context.__exit__, None, None, None)
        logging.getLogger("paramiko").setLevel(logging.CRITICAL)
        self.password = "test-only-local-password"
        self.mode, self.challenge = "password", "Password:"
        self.auth_attempts = self.network_connections = self.keepalives = 0
        self.commands, self.responses, self.transports = [], [], []
        self.accept_env = None
        self.stop = threading.Event()
        self.key = integration.paramiko.RSAKey.generate(2048)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(5)
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.known_hosts = self.directory / "known hosts"
        self.known_hosts.write_text(
            "[127.0.0.1]:%d %s %s\n"
            % (self.port, self.key.get_name(), self.key.get_base64()),
            encoding="utf-8",
        )
        ssh_config = self.directory / "ssh config"
        ssh_config.write_text(
            "Host local-alias\n HostName 127.0.0.1\n User alice\n Port %d\nHost *\n IdentityAgent none\n GlobalKnownHostsFile NUL\n"
            % self.port,
            encoding="utf-8",
        )
        self.config = Config(
            "local-alias",
            password=self.password,
            auth_mode="password",
            known_hosts_file=str(self.known_hosts),
            ssh_config_file=str(ssh_config),
            connect_timeout=3,
            reconnect_delay=2,
        )
        self.acceptor = threading.Thread(
            target=integration.OpenSSHIntegrationTests.accept, args=(self,), daemon=True
        )
        self.acceptor.start()
        self.addCleanup(self.close)

    def close(self):
        self.stop.set()
        self.listener.close()
        for session in self.transports:
            session.close()
        self.acceptor.join(timeout=2)

    def execute(self, command, config=None, **kwargs):
        return SSHClient(
            config or self.config, control_dir=self.directory / "state"
        ).execute(command, timeout=4, **kwargs)

    def test_password_alias_unicode_multiline_and_secret_redaction(self):
        command = "printf '中文';\nprintf 'two words'"
        result = self.execute(command)
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(self.commands, [command])
        self.assertEqual(result.stdout, command + "\n")
        result = self.execute("echo-secret")
        self.assertEqual(result.stdout, "[REDACTED]\n")
        self.assertEqual(self.network_connections, 2)

    def test_keyboard_interactive_and_otp_refusal(self):
        self.mode = "keyboard-interactive"
        config = dataclasses.replace(self.config, auth_mode="keyboard-interactive")
        self.assertEqual(self.execute("pwd", config).exit_code, 0)
        self.challenge = "OTP:"
        result = self.execute("never", config)
        self.assertEqual(result.exit_code, 255)
        self.assertEqual(self.commands, ["pwd"])
        self.assertNotIn([self.password], self.responses[1:])

    def test_private_key_and_host_key_refusal(self):
        self.mode = "key"
        key_file = self.directory / "private key"
        with os.fdopen(
            open_private(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL),
            "w",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            self.key.write_private_key(handle)
        config = dataclasses.replace(
            self.config, password="", auth_mode="key", identity_file=str(key_file)
        )
        result = self.execute("pwd", config)
        self.assertEqual(result.exit_code, 0, result.stderr)
        empty_hosts = self.directory / "empty hosts"
        empty_hosts.write_text("")
        result = self.execute(
            "never", dataclasses.replace(config, known_hosts_file=str(empty_hosts))
        )
        self.assertEqual(result.exit_code, 255)
        self.assertEqual(self.commands, ["pwd"])

    def test_wrong_password_cools_down_without_command_or_replay(self):
        config = dataclasses.replace(self.config, password="wrong-test-password")
        result = self.execute("never", config)
        self.assertEqual(result.exit_code, 255)
        with self.assertRaises(ToolError):
            self.execute("never", config)
        self.assertEqual(self.network_connections, 1)
        self.assertEqual(self.commands, [])

    def test_no_input_sends_eof_and_file_input_preserves_all_bytes(self):
        result = self.execute("stdin-eof-test")
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), hashlib.sha256(b"").hexdigest())
        data = bytes(range(256)) * 300
        source = self.directory / "binary stdin"
        source.write_bytes(data)
        result = self.execute("stdin-eof-test", stdin_file=source)
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), hashlib.sha256(data).hexdigest())


if __name__ == "__main__":
    unittest.main()
