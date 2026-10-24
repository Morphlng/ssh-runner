"""Agent workflows exercised against real OpenSSH and a disposable local server."""

import contextlib
import dataclasses
import io
import json
import os
import shlex
import sys
import time
import unittest
from pathlib import Path

import test_integration as integration
from support import client, run_command, run_job, transfer, wait_job

from ssh_runner import ToolError, cli
from ssh_runner import config as config_module
from ssh_runner.ssh.process import Capture


class CaptureTests(unittest.TestCase):
    def test_split_secrets_are_redacted_before_preview_and_disk(self):
        for width in (1, 2, 5, 11, 64):
            output = io.BytesIO()
            capture = Capture(18, b"secret-value", output)
            data = b"prefix secret-valuesecret-value suffix"
            for start in range(0, len(data), width):
                capture.feed(data[start : start + width])
            capture.feed(b"", final=True)
            self.assertEqual(output.getvalue(), b"prefix [REDACTED][REDACTED] suffix")
            self.assertEqual(capture.text, "prefix [REDACTED][REDACTED] suffix"[:18])
            self.assertTrue(capture.truncated)
            self.assertEqual(capture.total, len(data))

    def test_repeated_single_byte_secret_and_utf8_boundary(self):
        sink = io.BytesIO()
        capture = Capture(8, b"x", sink)
        capture.feed(b"x" * 65536)
        capture.feed(b"", final=True)
        self.assertEqual(sink.getvalue(), b"[REDACTED]" * 65536)
        capture = Capture(100, "密码".encode())
        for byte in "前密码后".encode():
            capture.feed(bytes([byte]))
        capture.feed(b"", final=True)
        self.assertEqual(capture.text, "前[REDACTED]后")


@unittest.skipUnless(
    os.name == "posix" and integration.paramiko is not None,
    "POSIX end-to-end suite requires Paramiko",
)
class AgentWorkflows(unittest.TestCase):
    def setUp(self):
        self.server = integration.OpenSSHIntegrationTests()
        self.server.setUp()
        self.addCleanup(self.server.tearDown)
        self.server.execute_shell = True
        self.config = self.server.config
        self.directory = self.server.directory
        self.common = dict(control_dir=self.server.control_dir, timeout=10)
        self.server.manager()

    def run_command(self, command, **kwargs):
        result = run_command(self.config, command, **self.common, **kwargs)
        self.assertEqual(result.exit_code, 0, result.stderr)
        return result

    def job(self, operation, **kwargs):
        result = run_job(self.config, operation, **self.common, **kwargs)
        self.assertEqual(result.exit_code, 0, result.stderr + result.stdout)
        return result.data

    def terminal(self, identifier):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            value = self.job("status", job_id=identifier)
            if value["state"] not in ("queued", "running"):
                return value
            time.sleep(0.1)
        self.fail("job did not terminate")

    def test_large_output_bounded_preview_and_private_complete_logs(self):
        out = self.directory / "out.log"
        err = self.directory / "err.log"
        command = "python3 -c " + shlex.quote(
            "import sys; print('x' * 200000); print('y' * 200000, file=sys.stderr)"
        )
        result = self.run_command(
            command, max_output_bytes=100, stdout_file=out, stderr_file=err
        )
        self.assertEqual(len(result.stdout), 100)
        self.assertEqual(len(result.stderr), 100)
        self.assertTrue(result.stdout_truncated and result.stderr_truncated)
        self.assertEqual(result.stdout_bytes, 200001)
        self.assertEqual(out.stat().st_size, 200001)
        self.assertEqual(err.stat().st_size, 200001)
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ToolError):
            self.run_command("printf overwritten", stdout_file=out)
        self.assertEqual(out.stat().st_size, 200001)

    def test_script_stdin_cwd_and_failure_does_not_execute_in_wrong_directory(self):
        cwd = self.directory / "a 'quoted; directory"
        cwd.mkdir()
        script = self.directory / "script.sh"
        script.write_text('printf "%s\\n" "$PWD"\nprintf done\n')
        result = self.run_command("sh -s", stdin_file=script, cwd=str(cwd))
        self.assertEqual(Path(result.stdout.splitlines()[0]).resolve(), cwd.resolve())
        self.assertTrue(result.stdout.endswith("done"))
        result = run_command(
            self.config,
            "touch should-not-exist; printf bad",
            cwd="/nonexistent-ssh-runner-directory",
            **self.common,
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse((self.directory / "should-not-exist").exists())

    def test_cli_script_arguments_over_openssh(self):
        cwd = self.directory / "script 'working directory"
        cwd.mkdir()
        script = self.directory / "argument script.sh"
        script.write_text('printf "%s\\n" "$PWD"\nprintf "%s\\000" "$@"\n')
        config_file = self.directory / "cli-config.json"
        config_file.write_text(config_module.serialize_config(self.config))
        config_file.chmod(0o600)
        marker = cwd / "injected"
        values = [
            "two words",
            "'quoted\"",
            "",
            "中文",
            "line\nbreak",
            "--apply",
            "$(touch injected)",
            "; touch injected",
        ]
        arguments = [
            "--config",
            str(config_file),
            "script",
            str(script),
            "--control-dir",
            str(self.server.control_dir),
            "--cwd",
            str(cwd),
            "--timeout",
            "10",
        ] + ["--arg=" + value for value in values]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(arguments)
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0, result["stderr"])
        directory, arguments_output = result["stdout"].split("\n", 1)
        self.assertEqual(Path(directory).resolve(), cwd.resolve())
        self.assertEqual(arguments_output, "".join(value + "\x00" for value in values))
        self.assertFalse(marker.exists())

    def test_binary_transfer_atomicity_hash_and_quoted_paths(self):
        source = self.directory / "source"
        data = bytes(range(256)) * 1000 + self.config.password.encode()
        source.write_bytes(data)
        remote = self.directory / "target ' ; file"
        result = transfer(
            self.config, "files", "upload", source, str(remote), **self.common
        )
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(remote.read_bytes(), data)
        source.write_bytes(b"changed")
        result = transfer(
            self.config, "files", "upload", source, str(remote), **self.common
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(remote.read_bytes(), data)
        local = self.directory / "download"
        result = transfer(
            self.config, "files", "download", str(remote), local, **self.common
        )
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(local.read_bytes(), data)
        result = transfer(
            self.config,
            "files",
            "download",
            "missing",
            local,
            overwrite=True,
            **self.common,
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(local.read_bytes(), data)
        result = transfer(
            self.config,
            "files",
            "upload",
            source,
            str(remote),
            overwrite=True,
            mode="700",
            **self.common,
        )
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(remote.read_bytes(), b"changed")
        self.assertEqual(remote.stat().st_mode & 0o777, 0o700)

    def test_corrupt_transfers_never_publish_or_overwrite(self):
        source = self.directory / "source"
        source.write_bytes(b"original-content")
        remote = self.directory / "remote"
        self.server.corrupt_stdin = True
        result = transfer(
            self.config, "files", "upload", source, str(remote), **self.common
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(remote.exists())
        self.assertFalse(list(self.directory.glob("*.ssh-runner.*")))
        self.server.corrupt_stdin = False
        remote.write_bytes(b"remote-data")
        local = self.directory / "download"
        local.write_bytes(b"keep-existing")
        self.server.stdout_prefix = b"login banner\n"
        with self.assertRaisesRegex(ToolError, "SHA-256"):
            transfer(
                self.config,
                "files",
                "download",
                str(remote),
                local,
                overwrite=True,
                **self.common,
            )
        self.assertEqual(local.read_bytes(), b"keep-existing")
        self.assertFalse(list(self.directory.glob(".ssh-download-*")))

    def test_stale_job_reports_unknown_and_cannot_be_deleted(self):
        identifier = "d" * 32
        self.job("start", job_id=identifier, command="true")
        self.terminal(identifier)
        path = self.directory / ".ssh-runner-jobs" / identifier / "status.json"
        value = json.loads(path.read_text())
        value.update(state="running", heartbeat_at=time.time() - 30)
        path.write_text(json.dumps(value))
        self.assertEqual(self.job("status", job_id=identifier)["state"], "unknown")
        result = run_job(self.config, "delete", job_id=identifier, **self.common)
        self.assertNotEqual(result.exit_code, 0)
        self.assertTrue(path.exists())

    def test_jobs_persist_exit_code_logs_and_idempotency(self):
        identifier = "a" * 32
        command = "printf run >> count; printf abcdef; printf problem >&2; exit 7"
        started = self.job("start", job_id=identifier, command=command)
        self.assertEqual(started["job_id"], identifier)
        self.server.manager().close()  # Job survives loss of its SSH connection.
        finished = self.terminal(identifier)
        self.assertEqual((finished["state"], finished["exit_code"]), ("finished", 7))
        page = self.job("logs", job_id=identifier, offset=2, limit=3)
        self.assertEqual((page["text"], page["next_offset"]), ("cde", 5))
        self.assertEqual(
            self.job("logs", job_id=identifier, stream="stderr")["text"], "problem"
        )
        self.job("start", job_id=identifier, command=command)
        self.assertEqual((self.directory / "count").read_text(), "run")
        self.assertIn(identifier, self.job("list")["job_ids"])
        self.assertTrue(self.job("delete", job_id=identifier)["deleted"])
        self.assertNotIn(identifier, self.job("list")["job_ids"])
        self.assertEqual(
            self.job("start", job_id=identifier, command=command)["state"], "deleted"
        )
        self.assertEqual((self.directory / "count").read_text(), "run")

    def test_job_cancel_timeout_and_log_limit(self):
        identifier = "b" * 32
        self.job(
            "start",
            job_id=identifier,
            command="printf 123456789; sleep 20",
            log_limit=4,
            job_timeout=6,
        )
        try:
            time.sleep(0.3)
            cancelled = self.job("cancel", job_id=identifier)
            self.assertTrue(cancelled["cancel_requested"])
            status = self.terminal(identifier)
            self.assertEqual(status["state"], "cancelled")
            self.assertTrue(status["stdout_truncated"])
            self.assertEqual(self.job("logs", job_id=identifier)["text"], "1234")
        finally:
            self.job("cancel", job_id=identifier)
        identifier = "c" * 32
        self.job(
            "start",
            job_id=identifier,
            command="trap '' TERM; sleep 20",
            job_timeout=0.2,
        )
        self.assertEqual(self.terminal(identifier)["state"], "timed_out")

    def test_directory_transfer_over_real_ssh_and_no_clobber(self):
        source = self.directory / "tree source"
        source.mkdir()
        (source / "empty").mkdir()
        (source / "中文 'file").write_bytes(bytes(range(256)) * 100)
        (source / "中文 'file").chmod(0o755)
        remote = self.directory / "remote tree"
        result = transfer(
            self.config, "directories", "upload", source, str(remote), **self.common
        )
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(result.data["files"], 1)
        destination = self.directory / "download tree"
        result = transfer(
            self.config,
            "directories",
            "download",
            str(remote),
            destination,
            **self.common,
        )
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(
            (destination / "中文 'file").read_bytes(),
            (source / "中文 'file").read_bytes(),
        )
        self.assertEqual((destination / "中文 'file").stat().st_mode & 0o777, 0o755)
        self.assertTrue((destination / "empty").is_dir())
        result = transfer(
            self.config, "directories", "upload", source, str(remote), **self.common
        )
        self.assertNotEqual(result.exit_code, 0)
        with self.assertRaises(ToolError):
            transfer(
                self.config,
                "directories",
                "download",
                str(remote),
                destination,
                **self.common,
            )
        self.server.corrupt_stdin = True
        bad_target = self.directory / "bad tree"
        result = transfer(
            self.config, "directories", "upload", source, str(bad_target), **self.common
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(bad_target.exists())
        self.assertFalse(list(self.directory.glob(".ssh-directory-*")))

    def test_job_log_pages_redact_secrets_and_preserve_unicode_over_ssh(self):
        identifier = "f" * 32
        raw = self.directory / "input-log"
        raw.write_text("中文 " + self.config.password + " 完成\n")
        self.job("start", job_id=identifier, command="cat " + shlex.quote(str(raw)))
        self.terminal(identifier)
        pieces, offset = [], 0
        while True:
            result = run_job(
                self.config,
                "logs",
                job_id=identifier,
                offset=offset,
                limit=2,
                **self.common,
            )
            self.assertEqual(result.exit_code, 0, result.stderr)
            self.assertEqual(result.data, result.data)
            pieces.append(result.data["text"])
            offset = result.data["next_offset"]
            if offset >= result.data["log_size"]:
                break
        text = "".join(pieces)
        self.assertNotIn(self.config.password, text)
        self.assertEqual(text.replace("[REDACTED]", ""), "中文  完成\n")
        self.assertNotIn("�", text)

    def test_wait_follow_and_doctor_over_ssh(self):
        identifier = "1" * 32
        self.job("start", job_id=identifier, command="printf '中文 done'; exit 7")
        pages = []
        result = wait_job(
            self.config,
            identifier,
            follow=True,
            limit=2,
            poll_interval=0.02,
            wait_timeout=10,
            on_event=pages.append,
            **self.common,
        )
        self.assertEqual(result.exit_code, 7, result.stderr)
        self.assertEqual("".join(page.data["text"] for page in pages), "中文 done")
        self.assertTrue(result.data["wait_completed"])
        result = client(self.config, self.server.control_dir).doctor(timeout=10)
        self.assertEqual(result.exit_code, 0, result.stdout + result.stderr)
        self.assertTrue(all(result.data["capabilities"].values()))

    def test_configured_interpreter_and_job_root_with_spaces(self):
        interpreter = self.directory / "python custom"
        interpreter.symlink_to(sys.executable)
        root = self.directory / "jobs 'custom root"
        self.config = dataclasses.replace(
            self.config, remote_python=str(interpreter), job_root=str(root)
        )
        identifier = "c" * 32
        self.job("start", job_id=identifier, command="printf custom")
        self.assertEqual(self.terminal(identifier)["exit_code"], 0)
        self.assertEqual(self.job("logs", job_id=identifier)["text"], "custom")
        self.assertTrue((root / identifier / "status.json").exists())
        result = client(self.config, self.server.control_dir).doctor(timeout=10)
        self.assertTrue(result.data["capabilities"]["jobs"], result.data)

    def test_existing_job_data_is_read_and_deleted_without_reexecution(self):
        root = self.directory / "legacy-jobs"
        identifier = "d" * 32
        directory = root / identifier
        directory.mkdir(parents=True, mode=0o700)
        root.chmod(0o700)
        (directory / "status.json").write_text(
            json.dumps({"job_id": identifier, "state": "finished", "exit_code": 9})
        )
        (directory / "stdout").write_bytes("old 中文\n".encode())
        self.config = dataclasses.replace(self.config, job_root=str(root))
        self.assertEqual(self.job("status", job_id=identifier)["exit_code"], 9)
        self.assertEqual(self.job("logs", job_id=identifier)["text"], "old 中文\n")
        self.assertTrue(self.job("delete", job_id=identifier)["deleted"])
        self.assertEqual(
            self.job("start", job_id=identifier, command="touch should-not-run")[
                "state"
            ],
            "deleted",
        )
        self.assertFalse((self.directory / "should-not-run").exists())
