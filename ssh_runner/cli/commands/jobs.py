"""Job subcommands expose only their applicable arguments."""

from ...core.defaults import (
    JOB_TIMEOUT,
    LOG_LIMIT,
    PAGE_LIMIT,
    POLL_INTERVAL,
    WAIT_TIMEOUT,
)
from .base import Command, remote_arguments, values

FIELDS = {
    "start": ("job_id", "cwd", "job_timeout", "log_limit"),
    "status": (),
    "cancel": (),
    "delete": (),
    "logs": ("stream", "offset", "limit"),
    "list": ("offset", "limit"),
    "wait": ("wait_timeout", "poll_interval"),
    "follow": ("stream", "offset", "limit", "wait_timeout", "poll_interval"),
}
ARGUMENTS = {
    "job_id": {},
    "cwd": {},
    "job_timeout": dict(type=float, default=JOB_TIMEOUT),
    "log_limit": dict(type=int, default=LOG_LIMIT),
    "stream": dict(choices=("stdout", "stderr"), default="stdout"),
    "offset": dict(type=int, default=0),
    "limit": dict(type=int, default=PAGE_LIMIT),
    "wait_timeout": dict(type=float, default=WAIT_TIMEOUT),
    "poll_interval": dict(type=float, default=POLL_INTERVAL),
}


class JobCommand(Command):
    requirements = "Python 3.8+, POSIX"

    def __init__(self, action):
        self.action = action
        self.path = ("job", action)
        self.help = "后台任务：" + action

    def configure(self, parser):
        if self.action != "list":
            parser.add_argument(
                "target", help="命令字符串" if self.action == "start" else "job ID"
            )
        remote_arguments(parser)
        for name in FIELDS[self.action]:
            parser.add_argument("--" + name.replace("_", "-"), **ARGUMENTS[name])

    def run(self, context, options):
        kwargs = values(options, "timeout", *FIELDS[self.action])
        if self.action == "follow":
            kwargs["on_event"] = context.presenter.log_event
        args = () if self.action == "list" else (options.target,)
        return getattr(context.client.jobs, self.action)(*args, **kwargs)
