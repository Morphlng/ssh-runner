"""Explicit job operations with stable IDs and no automatic resubmission."""

import dataclasses
import math
import re
import time
import uuid

from ..core.defaults import (
    JOB_TIMEOUT,
    LOG_LIMIT,
    PAGE_LIMIT,
    POLL_INTERVAL,
    WAIT_TIMEOUT,
)
from ..core.errors import ToolError
from ..core.results import redact, with_data
from ..resources import remote_source
from .job_logs import TERMINAL_STATES, log_page


def _positive_seconds(name, value, maximum=None):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
        or (maximum is not None and value > maximum)
    ):
        raise ToolError(name + " 必须是范围内的大于零的有限秒数。")


def _page(offset, limit):
    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= 65536
    ):
        raise ToolError("分页需非负字节 offset 和 1 到 65536 的 limit。")


class JobManager:
    def __init__(self, executor, protocol):
        self.executor = executor
        self.protocol = protocol

    def _request(self, operation, job_id=None, *, payload=None, **options):
        if operation != "list" and (
            not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id)
        ):
            raise ToolError("job_id 必须是 32 位小写十六进制字符串。")
        config = self.executor.config
        source = remote_source("jobs")
        request = dict(payload or {}, operation=operation, job_id=job_id)
        if operation == "start":
            request["helper_source"] = source
        try:
            result, data = self.protocol.call(
                source, request, config.job_root, **options
            )
            if result.exit_code == 0 and operation == "logs":
                data = log_page(
                    data, config.secrets, request["offset"], request["limit"]
                )
            if data is not None:
                result = with_data(
                    result, "job." + operation, redact(data, config.secrets)
                )
            result = dataclasses.replace(
                result, operation="job." + operation, job_id=job_id
            )
        except (ToolError, KeyboardInterrupt) as error:
            error.job_id = job_id
            raise
        if result.exit_code != 0 and job_id is not None:
            return dataclasses.replace(
                result,
                stderr=result.stderr
                + "\njob_id="
                + job_id
                + "; query status before retrying\n",
            )
        return result

    def start(
        self,
        command,
        *,
        job_id=None,
        cwd=None,
        job_timeout=JOB_TIMEOUT,
        log_limit=LOG_LIMIT,
        **options,
    ):
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise ToolError("后台命令不能为空或包含 NUL。")
        if any(secret in command for secret in self.executor.config.secrets):
            raise ToolError("后台命令包含配置的密码，已拒绝执行。")
        if cwd is not None and (not isinstance(cwd, str) or not cwd or "\x00" in cwd):
            raise ToolError("cwd 必须是非空路径。")
        _positive_seconds("job_timeout", job_timeout, 604800)
        if type(log_limit) is not int or not 0 <= log_limit <= 1024 * 1024 * 1024:
            raise ToolError("log_limit 必须为 0 到 1 GiB 的整数。")
        return self._request(
            "start",
            uuid.uuid4().hex if job_id is None else job_id,
            payload=dict(
                command=command, cwd=cwd, job_timeout=job_timeout, log_limit=log_limit
            ),
            **options,
        )

    def status(self, job_id, **options):
        return self._request("status", job_id, **options)

    def cancel(self, job_id, **options):
        return self._request("cancel", job_id, **options)

    def delete(self, job_id, **options):
        return self._request("delete", job_id, **options)

    def list(self, *, offset=0, limit=PAGE_LIMIT, **options):
        _page(offset, limit)
        return self._request(
            "list", payload=dict(offset=offset, limit=limit), **options
        )

    def logs(self, job_id, *, stream="stdout", offset=0, limit=PAGE_LIMIT, **options):
        _page(offset, limit)
        if stream not in {"stdout", "stderr"}:
            raise ToolError("stream 必须是 stdout 或 stderr。")
        context = (
            max(
                (len(s.encode("utf-8")) for s in self.executor.config.secrets),
                default=1,
            )
            + 3
        )
        return self._request(
            "logs",
            job_id,
            payload=dict(
                stream=stream, offset=offset, limit=limit, context_bytes=context
            ),
            **options,
        )

    def wait(
        self,
        job_id,
        *,
        wait_timeout=WAIT_TIMEOUT,
        poll_interval=POLL_INTERVAL,
        **options,
    ):
        return self._wait(
            job_id, wait_timeout=wait_timeout, poll_interval=poll_interval, **options
        )

    def follow(
        self,
        job_id,
        *,
        stream="stdout",
        offset=0,
        limit=PAGE_LIMIT,
        wait_timeout=WAIT_TIMEOUT,
        poll_interval=POLL_INTERVAL,
        on_event=None,
        **options,
    ):
        return self._wait(
            job_id,
            follow=True,
            stream=stream,
            offset=offset,
            limit=limit,
            wait_timeout=wait_timeout,
            poll_interval=poll_interval,
            on_event=on_event,
            **options,
        )

    def _wait(
        self,
        job_id,
        *,
        follow=False,
        stream="stdout",
        offset=0,
        limit=16384,
        wait_timeout=300,
        poll_interval=1,
        on_event=None,
        **kwargs,
    ):
        """Wait without resubmission/cancellation; optionally emit bounded log pages.

        Unlike a status request, a completed wait returns the task's exit code.
        A local deadline returns 124 with error_kind=wait_timeout; the task continues.
        """
        for name, value in (
            ("wait_timeout", wait_timeout),
            ("poll_interval", poll_interval),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ToolError(name + " 必须是大于零的有限秒数。")
        deadline = time.monotonic() + wait_timeout
        last = None
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    from ..core.results import ExecutionResult

                    last = last or ExecutionResult(124, False, "", "", job_id=job_id)
                    data = dict(last.data or {})
                    data.update(
                        wait_completed=False, wait_timed_out=True, next_offset=offset
                    )
                    return with_data(
                        last,
                        "job.follow" if follow else "job.wait",
                        data,
                        exit_code=124,
                        timed_out=False,
                        error_kind="wait_timeout",
                    )
                call_options = dict(kwargs)
                call_options["timeout"] = min(
                    call_options.get("timeout", 30), remaining
                )
                last = (
                    self.logs(
                        job_id,
                        stream=stream,
                        offset=offset,
                        limit=limit,
                        **call_options,
                    )
                    if follow
                    else self.status(job_id, **call_options)
                )
                if last.exit_code != 0:
                    return last
                data = last.data
                state = data.get("state")
                previous = offset
                if follow:
                    offset = data["next_offset"]
                    if on_event is not None and (data["text"] or offset != previous):
                        on_event(last)
                if state in TERMINAL_STATES:
                    if follow and offset < data["log_size"] and offset > previous:
                        continue
                    code = data.get("exit_code")
                    if state == "cancelled":
                        code = 130
                    elif state == "timed_out":
                        code = 124
                    elif state in {"failed", "deleted"}:
                        code = 1
                    if type(code) is not int or not 0 <= code <= 255:
                        code = 1
                    data = dict(
                        data,
                        wait_completed=True,
                        wait_timed_out=False,
                        next_offset=offset,
                    )
                    return with_data(
                        last,
                        "job.follow" if follow else "job.wait",
                        data,
                        exit_code=code,
                    )
                if state not in {"queued", "running"}:
                    return with_data(
                        last,
                        "job.follow" if follow else "job.wait",
                        dict(data, wait_completed=False, next_offset=offset),
                        exit_code=1,
                        error_kind="job_unknown",
                    )
                if follow and offset > previous:
                    continue
                time.sleep(max(0, min(poll_interval, deadline - time.monotonic())))
        except (ToolError, KeyboardInterrupt) as error:
            error.job_id = job_id
            raise
