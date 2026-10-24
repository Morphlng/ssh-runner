"""Explicit release acceptance: native Windows client -> WSL loopback POSIX peer.

Install .[test] in both environments first, then on Windows run:
  python tests/verify_windows_posix.py --distribution Ubuntu-24.04 \
      --linux-python /path/to/disposable/venv/bin/python
No system sshd, real credentials, sudoers changes or external peers are used.
"""

import argparse
import dataclasses
import io
import json
import os
import subprocess
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def serve():
    import test_integration as integration

    server = integration.OpenSSHIntegrationTests()
    server.setUp()
    server.execute_shell = True
    try:
        # A fixture sudo validates password stdin and target-stdin isolation;
        # never alter the machine's sudo policy or use an account password.
        binary = server.directory / "bin"
        binary.mkdir()
        sudo = binary / "sudo"
        sudo.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = '-S' ]; then IFS= read -r password; "
            "[ \"$password\" = 'fixture-sudo-password' ] || exit 1; fi\n"
            'while [ "$1" != \'--\' ]; do shift; done\nshift\nexec "$@"\n',
            encoding="utf-8",
        )
        sudo.chmod(0o700)
        os.environ["PATH"] = str(binary) + os.pathsep + os.environ["PATH"]
        print(
            json.dumps(
                dict(
                    port=server.port,
                    key_type=server.key.get_name(),
                    key=server.key.get_base64(),
                    root=str(server.directory),
                )
            ),
            flush=True,
        )
        sys.stdin.readline()
    finally:
        print(
            "fixture sessions=%d auth=%d commands=%d"
            % (server.network_connections, server.auth_attempts, len(server.commands)),
            file=sys.stderr,
            flush=True,
        )
        server.tearDown()


def verify(options):
    from ssh_runner import Config, SSHClient, ToolError
    from ssh_runner._platform import private_temporary_directory

    repo = Path(__file__).resolve().parents[1]
    linux_repo = subprocess.check_output(
        [
            "wsl.exe",
            "-d",
            options.distribution,
            "--exec",
            "wslpath",
            "-a",
            "-u",
            str(repo),
        ],
        encoding="utf-8",
        timeout=15,
    ).strip()
    with private_temporary_directory(
        prefix="windows-posix-"
    ) as directory, tempfile.TemporaryFile() as errors:
        root = Path(directory)
        server = subprocess.Popen(
            [
                "wsl.exe",
                "-d",
                options.distribution,
                "--exec",
                options.linux_python,
                linux_repo + "/tests/verify_windows_posix.py",
                "--serve",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
            encoding="utf-8",
        )
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                startup = pool.submit(server.stdout.readline)
                try:
                    peer = json.loads(startup.result(timeout=20))
                except BaseException:
                    server.terminate()
                    raise
            known = root / "known hosts"
            known.write_text(
                "[127.0.0.1]:%d %s %s\n"
                % (peer["port"], peer["key_type"], peer["key"]),
                encoding="utf-8",
            )
            ssh_config = root / "ssh config"
            ssh_config.write_text(
                "Host *\n IdentityAgent none\n GlobalKnownHostsFile NUL\n",
                encoding="utf-8",
            )
            config = Config(
                "127.0.0.1",
                "alice",
                peer["port"],
                "test-only-local-password",
                auth_mode="password",
                known_hosts_file=str(known),
                ssh_config_file=str(ssh_config),
                sudo_password="fixture-sudo-password",
                job_root=peer["root"] + "/job root",
            )
            client = SSHClient(config, control_dir=root / "state")

            def success(result):
                assert result.exit_code == 0, result
                return result

            assert success(client.execute("printf '中文'", timeout=10)).stdout == "中文"
            script = root / "literal script.sh"
            script.write_bytes(b'printf "%s\\n" "$1"\n')
            assert (
                success(
                    client.script(script, args=["two words; $literal"], timeout=10)
                ).stdout
                == "two words; $literal\n"
            )
            assert (
                success(
                    client.execute("cat; printf sudo-ok", sudo=True, timeout=10)
                ).stdout
                == "sudo-ok"
            )
            source = root / "binary file"
            source.write_bytes(bytes(range(256)) * 100)
            remote = peer["root"] + "/remote file"
            success(client.files.upload(source, remote, timeout=10))
            downloaded = root / "downloaded"
            success(client.files.download(remote, downloaded, timeout=10))
            assert downloaded.read_bytes() == source.read_bytes()
            try:
                client.files.download(remote, downloaded, timeout=10)
            except ToolError:
                pass
            else:
                raise AssertionError("existing download target was overwritten")
            tree = root / "tree"
            tree.mkdir()
            (tree / "中文").write_bytes(b"directory data")
            remote_tree = peer["root"] + "/remote tree"
            success(client.directories.upload(tree, remote_tree, timeout=10))
            success(
                client.directories.download(remote_tree, root / "tree copy", timeout=10)
            )
            assert (root / "tree copy/中文").read_bytes() == b"directory data"
            identifier = uuid.uuid4().hex
            success(
                client.jobs.start(
                    "printf job-ok; exit 7", job_id=identifier, timeout=10
                )
            )
            result = client.jobs.wait(
                identifier, wait_timeout=10, poll_interval=0.1, timeout=5
            )
            assert result.exit_code == 7, result
            assert (
                success(client.jobs.logs(identifier, timeout=5)).data["text"]
                == "job-ok"
            )
            events = []
            result = client.jobs.follow(
                identifier,
                wait_timeout=5,
                poll_interval=0.1,
                timeout=5,
                on_event=events.append,
            )
            assert result.exit_code == 7 and events, result
            long_job = uuid.uuid4().hex
            success(client.jobs.start("sleep 30", job_id=long_job, timeout=5))
            success(client.jobs.cancel(long_job, timeout=5))
            cancelled = client.jobs.wait(
                long_job, wait_timeout=5, poll_interval=0.1, timeout=5
            )
            assert (
                cancelled.exit_code == 130 and cancelled.data["state"] == "cancelled"
            ), cancelled
            success(client.doctor(timeout=10))
            print(
                "Windows -> WSL POSIX acceptance passed: exec, literal script, sudo fixture, binary files, directories, jobs, logs/follow/cancel, doctor."
            )
        finally:
            if server.poll() is None:
                try:
                    server.communicate("stop\n", timeout=10)
                except subprocess.TimeoutExpired:
                    server.terminate()
                    server.communicate(timeout=5)
            errors.seek(0)
            diagnostics = errors.read().decode("utf-8", "replace")
            if sys.exc_info()[0] is not None:
                print(diagnostics, file=sys.stderr)
            if server.returncode:
                raise RuntimeError(diagnostics)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--distribution", default="Ubuntu-24.04")
    parser.add_argument("--linux-python")
    options = parser.parse_args()
    if options.serve:
        serve()
    elif os.name == "nt" and options.linux_python:
        verify(options)
    else:
        parser.error(
            "Run on Windows with --linux-python pointing to a WSL .[test] environment."
        )


if __name__ == "__main__":
    main()
