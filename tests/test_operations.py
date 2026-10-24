"""Regression coverage for safe log pages, process groups and directory archives."""

import base64
import io
import json
import os
import shlex
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ssh_runner import Config, ExecutionResult, SSHClient, ToolError
from ssh_runner.core.results import with_data
from ssh_runner.remote import archive
from ssh_runner.remote import jobs as remote_jobs
from ssh_runner.services.job_logs import log_page
from ssh_runner.services.protocol import RemoteProtocol


class LogPageTests(unittest.TestCase):
    def page(self, text, offset, limit, secrets=(), state="finished"):
        raw = text.encode()
        context = max((len(s.encode()) for s in secrets), default=1) + 3
        start = max(0, min(offset, len(raw)) - context)
        end = min(len(raw), offset + limit + context)
        return log_page(
            dict(
                state=state,
                data_b64=base64.b64encode(raw[start:end]).decode(),
                window_offset=start,
                log_size=len(raw),
            ),
            secrets,
            offset,
            limit,
        )

    def test_unicode_roundtrips_at_every_page_width(self):
        original = "中文 abc 🎈 é\n" * 10
        for width in range(1, 32):
            offset, chunks = 0, []
            while offset < len(original.encode()):
                page = self.page(original, offset, width)
                self.assertGreater(page["next_offset"], offset)
                chunks.append(page["text"])
                offset = page["next_offset"]
            self.assertEqual("".join(chunks), original)
        self.assertEqual(self.page("中文", 1, 3)["text"], "文")

    def test_secret_is_hidden_for_every_cursor_and_page_width(self):
        secrets = ("demo-secret-917", '含"引号\\的密码')
        for secret in secrets:
            for width in range(1, len(secret.encode()) + 3):
                offset, chunks = 0, []
                while offset < len(secret.encode()):
                    page = self.page(secret, offset, width, secrets)
                    chunks.append(page["text"])
                    self.assertEqual(page["text"], "[REDACTED]")
                    offset = page["next_offset"]
                self.assertNotIn(secret, "".join(chunks))

    def test_running_tail_is_withheld_and_truncated_prefix_is_masked(self):
        secret = "demo-secret-917"
        first = self.page("start " + secret[:5], 0, 100, (secret,), "running")
        self.assertEqual(first["text"], "")
        final = self.page("start " + secret, first["next_offset"], 100, (secret,))
        self.assertEqual(final["text"], "start [REDACTED]")
        self.assertEqual(
            self.page("start " + secret[:5], 0, 100, (secret,))["text"],
            "start [REDACTED]",
        )

    def test_overlapping_secrets_and_empty_out_of_range_pages(self):
        self.assertNotIn(
            "a",
            self.page("ababa", 0, 5, ("aba", "bab"))["text"].replace("[REDACTED]", ""),
        )
        self.assertEqual(self.page("", 10, 1)["next_offset"], 10)
        self.assertEqual(self.page("abc", 10, 1)["text"], "")


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_binary_empty_directories_unicode_and_executable_modes_roundtrip(self):
        source = self.root / "source"
        source.mkdir()
        (source / "空目录").mkdir()
        (source / "a 'file").write_bytes(bytes(range(256)) * 100)
        (source / "a 'file").chmod(0o755)
        with tempfile.TemporaryFile() as stream:
            archive.pack_directory(source, stream)
            data = archive.unpack_directory(stream, self.root / "destination")
        target = self.root / "destination"
        self.assertEqual(
            (target / "a 'file").read_bytes(), (source / "a 'file").read_bytes()
        )
        self.assertEqual((target / "a 'file").stat().st_mode & 0o777, 0o755)
        self.assertTrue((target / "空目录").is_dir())
        self.assertEqual(data["files"], 1)

    def test_symlinks_and_fifos_rejected_when_packing(self):
        source = self.root / "source"
        source.mkdir()
        link = source / "link"
        link.symlink_to(self.root)
        with tempfile.TemporaryFile() as stream, self.assertRaises(ValueError):
            archive.pack_directory(source, stream)
        link.unlink()
        os.mkfifo(link)
        with tempfile.TemporaryFile() as stream, self.assertRaises(ValueError):
            archive.pack_directory(source, stream)

    def test_hostile_archives_never_escape_or_publish(self):
        for name, kind in (
            ("../escape", tarfile.REGTYPE),
            ("/escape", tarfile.REGTYPE),
            ("link", tarfile.SYMTYPE),
            ("hard", tarfile.LNKTYPE),
            ("fifo", tarfile.FIFOTYPE),
        ):
            with self.subTest(name=name), io.BytesIO() as stream:
                with tarfile.open(fileobj=stream, mode="w") as writer:
                    entry = tarfile.TarInfo(name)
                    entry.type = kind
                    entry.linkname = "../escape"
                    writer.addfile(entry)
                stream.seek(0)
                with self.assertRaises(ValueError):
                    archive.unpack_directory(stream, self.root / "out")
                self.assertFalse((self.root / "out").exists())
                self.assertFalse((self.root / "escape").exists())
                self.assertFalse(list(self.root.glob(".ssh-directory-*")))

    def test_publication_refuses_existing_empty_directory_and_symlink(self):
        for symlink in (False, True):
            stage = self.root / "stage"
            stage.mkdir()
            destination = self.root / "destination"
            if symlink:
                destination.symlink_to(self.root / "missing")
            else:
                destination.mkdir()
            with self.assertRaises(OSError):
                archive.publish_directory(stage, destination)
            self.assertTrue(stage.exists())
            if symlink:
                self.assertTrue(destination.is_symlink())
                destination.unlink()
            else:
                destination.rmdir()
            stage.rmdir()

    def test_corrupt_upload_never_publishes(self):
        target = self.root / "target"
        completed = subprocess.run(
            [sys.executable, archive.__file__, "receive", str(target), "4", "0" * 64],
            input=b"bad!",
            capture_output=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(target.exists())


class WorkerTests(unittest.TestCase):
    def test_timeout_and_cancel_kill_descendants_after_shell_exits(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                child_info, heartbeat = root / "child.json", root / "heartbeat"
                child = (
                    "import os,signal,time,json\n"
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                    "open(%r,'w').write(json.dumps([os.getpid(),os.getpgrp()]))\n"
                    "while True:\n open(%r,'w').write(str(time.time()))\n time.sleep(.03)"
                ) % (str(child_info), str(heartbeat))
                parent = (
                    "import subprocess,sys; subprocess.Popen([sys.executable,'-c',%r])"
                    % child
                )
                request = dict(
                    command=shlex.quote(sys.executable) + " -c " + shlex.quote(parent),
                    cwd=temp,
                    job_timeout=10 if cancel else 0.4,
                    log_limit=1024,
                )
                (root / "request.json").write_text(json.dumps(request))
                process = subprocess.Popen(
                    [sys.executable, remote_jobs.__file__, "worker", temp],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    deadline = time.monotonic() + 3
                    while not child_info.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(child_info.exists())
                    if cancel:
                        time.sleep(0.15)
                        (root / "cancel").touch()
                    _, stderr = process.communicate(timeout=8)
                    self.assertEqual(process.returncode, 0, stderr)
                    status = json.loads((root / "status.json").read_text())
                    self.assertEqual(
                        status["state"], "cancelled" if cancel else "timed_out"
                    )
                    self.assertEqual(status["exit_code"], 130 if cancel else 124)
                    value = heartbeat.read_text()
                    time.sleep(0.15)
                    self.assertEqual(
                        heartbeat.read_text(), value, "descendant is still running"
                    )
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()
                    if child_info.exists():
                        _, group = json.loads(child_info.read_text())
                        try:
                            os.killpg(group, signal.SIGKILL)
                        except ProcessLookupError:
                            pass


class ProtocolAndWaitTests(unittest.TestCase):
    def setUp(self):
        self.config = Config("example.com", "alice", 22, "demo-password")
        self.executor = mock.Mock(config=self.config)
        self.client = SSHClient(self.config, executor=self.executor)

    def result(self, **data):
        return with_data(
            ExecutionResult(0, False, "", "", job_id="a" * 32), "job.status", data
        )

    def test_private_protocol_preserves_json_before_sanitizing(self):
        data = {"text": 'quote"slash\\中文', "exit_code": 0}

        def run(*args, stdout_file, **kwargs):
            stdout_file.write(json.dumps(data).encode())
            return ExecutionResult(0, False, "", "")

        result, parsed = RemoteProtocol(
            mock.Mock(config=self.config, execute=run)
        ).receive("ignored")
        self.assertEqual(parsed, data)
        self.assertEqual(result.stdout, "")

        def invalid(*args, stdout_file, **kwargs):
            stdout_file.write(b"banner\n{bad}")
            return ExecutionResult(0, False, "", "")

        with self.assertRaises(ToolError):
            RemoteProtocol(mock.Mock(config=self.config, execute=invalid)).receive(
                "ignored"
            )

    def test_wait_returns_task_exit_code_and_does_not_resubmit(self):
        with mock.patch.object(
            self.client.jobs,
            "status",
            side_effect=[
                self.result(state="running"),
                self.result(state="finished", exit_code=7),
            ],
        ) as call:
            result = self.client.jobs.wait("a" * 32, poll_interval=0.001)
        self.assertEqual(result.exit_code, 7)
        self.assertTrue(result.data["wait_completed"])
        self.assertEqual(
            [entry.args[0] for entry in call.call_args_list], ["a" * 32, "a" * 32]
        )

    def test_wait_deadline_unknown_and_interrupt_keep_identity(self):
        with mock.patch.object(
            self.client.jobs, "status", return_value=self.result(state="running")
        ):
            result = self.client.jobs.wait(
                "a" * 32, wait_timeout=0.01, poll_interval=0.01
            )
        self.assertEqual(result.error_kind, "wait_timeout")
        self.assertEqual(result.job_id, "a" * 32)
        self.assertFalse(result.timed_out)
        with mock.patch.object(
            self.client.jobs, "status", return_value=self.result(state="unknown")
        ) as call:
            result = self.client.jobs.wait("a" * 32)
            call.assert_called_once()
        self.assertEqual(result.error_kind, "job_unknown")
        with mock.patch.object(
            self.client.jobs, "status", side_effect=KeyboardInterrupt()
        ):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.client.jobs.wait("a" * 32)
        self.assertEqual(caught.exception.job_id, "a" * 32)

    def test_follow_drains_terminal_log_pages_and_streams_without_aggregation(self):
        events = []
        pages = [
            self.result(
                state="finished", exit_code=0, next_offset=2, log_size=4, text="ab"
            ),
            self.result(
                state="finished", exit_code=0, next_offset=4, log_size=4, text="cd"
            ),
        ]
        with mock.patch.object(self.client.jobs, "logs", side_effect=pages) as call:
            result = self.client.jobs.follow("a" * 32, on_event=events.append)
        self.assertEqual([item.data["text"] for item in events], ["ab", "cd"])
        self.assertEqual(call.call_args.kwargs["offset"], 2)
        self.assertEqual(result.data["next_offset"], 4)

    def test_doctor_stages_and_dependency_gaps(self):
        output = "SSH_RUNNER_DOCTOR_V1\nsh=1\ncat=1\nwc=1\ntr=1\nsha256sum=1\npython_supported=0\n"
        with mock.patch.object(
            self.executor.transport, "resolve_config"
        ), mock.patch.object(
            self.executor, "execute", return_value=ExecutionResult(0, False, output, "")
        ):
            result = self.client.doctor()
        self.assertEqual(result.exit_code, 1)
        self.assertTrue(result.data["capabilities"]["download"])
        self.assertFalse(result.data["capabilities"]["jobs"])
        with mock.patch.object(
            self.executor.transport,
            "resolve_config",
            side_effect=ToolError("bad ssh config"),
        ), mock.patch.object(self.executor, "execute") as run:
            result = self.client.doctor()
            run.assert_not_called()
        self.assertEqual(result.error_kind, "openssh")
