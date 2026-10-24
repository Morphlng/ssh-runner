"""Standalone helper sent to the remote Python 3 interpreter; standard library only."""

import base64
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def write_json(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix="." + path.name + "-",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("job directory must be owned by current user with mode 0700")


def worker(directory):
    os.umask(0o077)
    request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
    status = {
        "job_id": directory.name,
        "state": "running",
        "started_at": time.time(),
        "exit_code": None,
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "stdout_truncated": False,
        "stderr_truncated": False,
    }
    process = None
    reason = None
    try:
        if (directory / "cancel").exists():
            status.update(state="cancelled", exit_code=130, finished_at=time.time())
            return
        process = subprocess.Popen(
            request["command"],
            shell=True,
            executable="/bin/sh",
            cwd=request.get("cwd"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        write_json(directory / "status.json", status)
        started = time.monotonic()
        stopping = None
        last_update = 0
        with (directory / "stdout").open("wb") as out, (directory / "stderr").open(
            "wb"
        ) as err, selectors.DefaultSelector() as selector:
            for pipe, name, handle in (
                (process.stdout, "stdout", out),
                (process.stderr, "stderr", err),
            ):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, (name, handle))
            while selector.get_map() or process.poll() is None:
                now = time.monotonic()
                if reason is None:
                    if (directory / "cancel").exists():
                        reason = "cancelled"
                    elif now - started >= request["job_timeout"]:
                        reason = "timed_out"
                    if reason:
                        # Do not reap the leader while inherited pipes remain open.
                        # Keeping its PID reserved lets us safely kill the group even
                        # after the shell exits, without signalling a reused PID.
                        if process.returncode is None:
                            try:
                                os.killpg(process.pid, signal.SIGTERM)
                            except ProcessLookupError:
                                pass
                        stopping = now
                if stopping is not None and now - stopping >= 2:
                    if process.returncode is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    if now - stopping >= 4:
                        break
                for key, _ in selector.select(0.1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    name, handle = key.data
                    before = status[name + "_bytes"]
                    status[name + "_bytes"] += len(chunk)
                    handle.write(chunk[: max(0, request["log_limit"] - before)])
                    handle.flush()
                    status[name + "_truncated"] = (
                        status[name + "_bytes"] > request["log_limit"]
                    )
                if now - last_update >= 1:
                    status["heartbeat_at"] = time.time()
                    write_json(directory / "status.json", status)
                    last_update = now
            process.wait(timeout=3)
        code = process.returncode
        status.update(
            state=reason or "finished",
            exit_code=(
                124
                if reason == "timed_out"
                else 130 if reason == "cancelled" else 128 - code if code < 0 else code
            ),
            process_exit_code=128 - code if code < 0 else code,
            finished_at=time.time(),
        )
    except BaseException as error:
        if process is not None and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        status.update(
            state="failed",
            error="worker failed (%s); check cwd and remote resources"
            % type(error).__name__,
            finished_at=time.time(),
        )
    finally:
        if process is not None:
            process.stdout.close()
            process.stderr.close()
        write_json(directory / "status.json", status)


def main(request, job_root="~/.ssh-runner-jobs"):
    os.umask(0o077)
    root = Path(job_root).expanduser().absolute()
    private_directory(root)
    operation = request["operation"]
    if operation == "list":
        entries = []
        for path in root.iterdir():
            if not re.fullmatch(r"[a-f0-9]{32}", path.name):
                continue
            try:
                deleted = (
                    json.loads((path / "status.json").read_text(encoding="utf-8")).get(
                        "state"
                    )
                    == "deleted"
                )
            except (OSError, ValueError):
                deleted = False
            if not deleted:
                entries.append(path.name)
        entries.sort()
        # Bound the JSON response independently of the user-supplied page size.
        end = min(len(entries), request["offset"] + min(request["limit"], 4096))
        return {"job_ids": entries[request["offset"] : end], "next_offset": end}
    directory = root / request["job_id"]
    if operation == "start":
        # mkdir is the atomic idempotency claim. Never resubmit an existing ID.
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            private_directory(directory)
        else:
            write_json(directory / "request.json", request)
            write_json(
                directory / "status.json",
                {
                    "job_id": directory.name,
                    "state": "queued",
                    "created_at": time.time(),
                    "exit_code": None,
                },
            )
            helper = directory / "worker.py"
            helper.write_text(request.pop("helper_source"), encoding="utf-8")
            subprocess.Popen(
                [sys.executable, str(helper), "worker", str(directory)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
    if not directory.exists():
        raise ValueError("unknown job_id")
    private_directory(directory)
    try:
        status = json.loads((directory / "status.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        status = {"job_id": directory.name, "state": "unknown", "exit_code": None}
    if operation == "delete":
        if status["state"] not in (
            "finished",
            "failed",
            "cancelled",
            "timed_out",
            "deleted",
        ):
            raise ValueError(
                "only completed jobs can be deleted; cancel and query first"
            )
        # Retain an idempotency tombstone: delayed retries must never rerun deleted jobs.
        write_json(
            directory / "status.json",
            {
                "job_id": directory.name,
                "state": "deleted",
                "exit_code": status.get("exit_code"),
            },
        )
        for name in ("request.json", "worker.py", "stdout", "stderr", "cancel"):
            try:
                (directory / name).unlink()
            except FileNotFoundError:
                pass
        return {"job_id": directory.name, "deleted": True}
    if operation == "cancel" and status["state"] in ("queued", "running", "unknown"):
        (directory / "cancel").touch(mode=0o600)
        status["cancel_requested"] = True
    if operation == "logs":
        name = request["stream"]
        path = directory / name
        if path.exists():
            with path.open("rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                context = request.get("context_bytes", 1026)
                start = max(0, min(request["offset"], size) - context)
                end = min(size, request["offset"] + request["limit"] + context)
                handle.seek(start)
                data = handle.read(max(0, end - start))
                status.update(
                    stream=name,
                    offset=request["offset"],
                    window_offset=start,
                    log_size=size,
                    data_b64=base64.b64encode(data).decode("ascii"),
                )
        else:
            status.update(
                stream=name,
                offset=request["offset"],
                window_offset=0,
                log_size=0,
                data_b64="",
            )
    heartbeat = status.get(
        "heartbeat_at", status.get("started_at", status.get("created_at", 0))
    )
    if status["state"] in ("running", "queued") and time.time() - heartbeat > 10:
        status["state"] = "unknown"
        status["error"] = "worker heartbeat is stale; execution outcome is unknown"
    return status


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "worker":
        worker(Path(sys.argv[2]))
    else:
        try:
            print(
                json.dumps(
                    main(
                        json.load(sys.stdin),
                        sys.argv[1] if len(sys.argv) > 1 else "~/.ssh-runner-jobs",
                    ),
                    ensure_ascii=False,
                )
            )
        except Exception as error:
            print(json.dumps({"error": str(error)}))
            sys.exit(2)
