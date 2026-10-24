"""Shared fixture construction for transport regression tests (not a public API)."""

from ssh_runner import SSHClient
from ssh_runner.ssh.connection import ConnectionManager as Manager
from ssh_runner.ssh.transport import OpenSSHTransport

transport = OpenSSHTransport()


def client(config, control_dir=None):
    return SSHClient(config, control_dir=control_dir, transport=transport)


def manager(config, *, control_dir=None):
    return Manager(config, control_dir=control_dir, transport=transport)


def run_command(config, command, timeout=30, *, control_dir=None, **kwargs):
    return client(config, control_dir).execute(command, timeout=timeout, **kwargs)


def run_job(config, action, *, control_dir=None, **kwargs):
    return getattr(client(config, control_dir).jobs, action)(**kwargs)


def wait_job(config, job_id, *, follow=False, control_dir=None, **kwargs):
    jobs = client(config, control_dir).jobs
    return (jobs.follow if follow else jobs.wait)(job_id, **kwargs)


def transfer(config, group, action, source, destination, *, control_dir=None, **kwargs):
    return getattr(getattr(client(config, control_dir), group), action)(
        source, destination, **kwargs
    )
