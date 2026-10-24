"""Bounded pipe draining and redaction, including secrets split across reads."""

import os
import queue
import selectors
import signal
import subprocess
import threading
import time
from typing import Tuple

from .._platform import WINDOWS
from ..core.defaults import STOP_GRACE_SECONDS

PIPE_CHUNK_BYTES = 65536
PIPE_QUEUE_CHUNKS = 8


class Capture:
    def __init__(self, limit, secret=b"", file=None, binary=False):
        self.limit = limit
        secrets = (secret,) if isinstance(secret, bytes) else tuple(secret)
        self.secrets = (
            tuple(sorted(set(secrets) - {b""}, key=lambda s: (-len(s), s)))
            if not binary
            else ()
        )
        self.file = file
        self.binary = binary
        self.pending = b""
        self.preview = bytearray()
        self.total = 0
        self.returned = 0

    def _emit(self, data):
        if self.file is not None:
            self.file.write(data)
            self.file.flush()
        if not self.binary:
            self.preview.extend(data[: max(0, self.limit - len(self.preview))])
            self.returned += len(data)

    def feed(self, data, final=False):
        self.total += len(data)
        data = self.pending + data
        if self.secrets:
            # Keep only a possible partial secret. Emit once per read, including
            # when output contains many repeated secrets (avoid quadratic copies).
            parts = []
            consumed = 0
            boundary = (
                len(data) if final else max(0, len(data) - len(self.secrets[0]) + 1)
            )
            while consumed < boundary:
                matches = [
                    (data.find(secret, consumed), secret) for secret in self.secrets
                ]
                matches = [(index, secret) for index, secret in matches if index >= 0]
                if not matches:
                    break
                index, secret = min(
                    matches, key=lambda match: (match[0], -len(match[1]))
                )
                if index >= boundary:
                    break
                parts.extend((data[consumed:index], b"[REDACTED]"))
                consumed = index + len(secret)
            end = max(consumed, boundary)
            parts.append(data[consumed:end])
            self.pending = data[end:]
            self._emit(b"".join(parts))
        else:
            self._emit(data)

    @property
    def text(self):
        return self.preview.decode("utf-8", errors="replace")

    @property
    def truncated(self):
        return self.returned > self.limit


def _signal_group(process: subprocess.Popen, signum: int) -> None:
    if WINDOWS:
        process.terminate_tree()
        return
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass
    except PermissionError:
        if process.poll() is None:
            raise


def _as_text(output) -> str:
    return (
        output.decode("utf-8", errors="replace")
        if isinstance(output, bytes)
        else output or ""
    )


class ProcessRunner:
    def __init__(self, grace=STOP_GRACE_SECONDS):
        self.grace = grace

    def run(self, *args, **kwargs):
        if WINDOWS:
            # Inspection/control subprocesses need the same tree ownership as exec.
            timeout = kwargs.pop("timeout", None)
            capture = kwargs.pop("capture_output", False)
            if capture:
                kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            with self.spawn(*args, **kwargs) as process:
                try:
                    out, err = process.communicate(timeout=timeout)
                except BaseException:
                    process.terminate_tree()
                    process.communicate(timeout=self.grace)
                    raise
                return subprocess.CompletedProcess(
                    args[0], process.returncode, out, err
                )
        return subprocess.run(*args, **kwargs)

    def spawn(self, *args, **kwargs):
        if WINDOWS:
            from .._platform.windows_process import WindowsProcess

            return WindowsProcess(*args, **kwargs)
        return subprocess.Popen(*args, **kwargs)

    def drain(self, process, stdout, stderr, timeout):
        """Drain both pipes with bounded memory; also bound inherited pipe lifetimes."""
        if WINDOWS:
            return self._drain_windows(process, stdout, stderr, timeout)
        grace = self.grace

        deadline = time.monotonic() + timeout
        timed_out = False
        terminate_at = None
        kill_at = None
        with selectors.DefaultSelector() as selector:
            try:
                for pipe, capture in (
                    (process.stdout, stdout),
                    (process.stderr, stderr),
                ):
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, capture)
                while selector.get_map() or process.poll() is None:
                    now = time.monotonic()
                    if not timed_out and now >= deadline:
                        timed_out = True
                        _signal_group(process, signal.SIGTERM)
                        terminate_at = now + grace
                    if terminate_at is not None and now >= terminate_at:
                        _signal_group(process, signal.SIGKILL)
                        terminate_at = None
                        kill_at = now + grace
                    if kill_at is not None and now >= kill_at:
                        break
                    for key, _ in selector.select(0.05):
                        try:
                            chunk = os.read(key.fd, 65536)
                        except BlockingIOError:
                            continue
                        if chunk:
                            key.data.feed(chunk)
                        else:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                process.wait(timeout=grace)
            except BaseException:
                _signal_group(process, signal.SIGTERM)
                try:
                    process.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    _signal_group(process, signal.SIGKILL)
                    process.wait(timeout=grace)
                raise
            finally:
                for pipe in (process.stdout, process.stderr):
                    pipe.close()
                stdout.feed(b"", final=True)
                stderr.feed(b"", final=True)
        return timed_out

    def _drain_windows(self, process, stdout, stderr, timeout):
        events = queue.Queue(maxsize=PIPE_QUEUE_CHUNKS)
        stopping = threading.Event()

        def send(event):
            while not stopping.is_set():
                try:
                    events.put(event, timeout=0.05)
                    return
                except queue.Full:
                    pass

        def read(pipe, capture):
            try:
                while not stopping.is_set():
                    chunk = pipe.read1(PIPE_CHUNK_BYTES)
                    if not chunk:
                        break
                    send((capture, chunk, None))
            except BaseException as error:
                send((capture, None, error))
            finally:
                send((capture, None, None))

        threads = [
            threading.Thread(target=read, args=pair, name="ssh-runner-pipe")
            for pair in ((process.stdout, stdout), (process.stderr, stderr))
        ]
        timed_out = False
        deadline = time.monotonic() + timeout
        done = 0
        try:
            for thread in threads:
                thread.start()
            while done < 2 or process.poll() is None:
                if time.monotonic() >= deadline:
                    timed_out = True
                    process.terminate_tree()
                    break
                try:
                    capture, data, error = events.get(timeout=0.05)
                except queue.Empty:
                    continue
                if error is not None:
                    raise error
                if data is None:
                    done += 1
                else:
                    capture.feed(data)
            if timed_out:
                # Killing the whole job closes inherited writer handles. Drain
                # the bounded remainder so partial output/redaction stays valid.
                until = time.monotonic() + self.grace
                while done < 2 and time.monotonic() < until:
                    try:
                        capture, data, error = events.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    if error is not None:
                        raise error
                    if data is None:
                        done += 1
                    else:
                        capture.feed(data)
            process.wait(timeout=self.grace)
        finally:
            process.close_job()  # Also removes descendants after a successful exit.
            stopping.set()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=self.grace)
            for pipe in (process.stdout, process.stderr):
                pipe.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=self.grace)
            stdout.feed(b"", final=True)
            stderr.feed(b"", final=True)
        return timed_out

    def stop(self, process: subprocess.Popen) -> Tuple[str, str]:
        """Bound shutdown even if a detached descendant keeps an output pipe open."""
        if WINDOWS:
            try:
                process.terminate_tree()
                return process.communicate(timeout=self.grace)
            finally:
                process.close_job()
        _signal_group(process, signal.SIGTERM)
        try:
            return process.communicate(timeout=self.grace)
        except subprocess.TimeoutExpired:
            _signal_group(process, signal.SIGKILL)
        try:
            return process.communicate(timeout=self.grace)
        except subprocess.TimeoutExpired as error:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            process.kill()
            process.wait(timeout=self.grace)
            return _as_text(error.stdout), _as_text(error.stderr)
