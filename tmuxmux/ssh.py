from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass

from .config import Host


@dataclass(frozen=True)
class SessionList:
    host: Host
    sessions: tuple[str, ...]
    error: str | None = None


SSH_OPTS = (
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=5",
    "-o", "ServerAliveInterval=30",
)


def _base_argv(host: Host) -> list[str]:
    """Argv that logs into `host`, up to (but not including) any remote command.

    - local=true → no ssh; we prepend the tmux binary directly.
    - host.command set → full shell invocation the user provided.
    - otherwise → plain `ssh <opts> target`.
    """
    if host.local:
        return []
    if host.command:
        return shlex.split(host.command)
    return ["ssh", *SSH_OPTS, host.ssh_target]


async def list_sessions(host: Host, timeout: float = 15.0) -> SessionList:
    """Run `ssh <host> tmux ls` (or just `tmux ls` for local) and parse names."""
    cmd = _base_argv(host) + ["tmux", "ls"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return SessionList(host, (), error="timeout")
    except FileNotFoundError:
        return SessionList(host, (), error="ssh not installed")

    if proc.returncode != 0:
        msg = stderr.decode(errors="replace").strip().splitlines()
        # tmux prints "no server running on ..." when there are zero sessions —
        # treat that as an empty list rather than an error.
        if any("no server running" in line for line in msg):
            return SessionList(host, ())
        return SessionList(host, (), error=msg[-1] if msg else f"exit {proc.returncode}")

    sessions = []
    for line in stdout.decode(errors="replace").splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        sessions.append(line.split(":", 1)[0])
    return SessionList(host, tuple(sessions))


def attach_command(host: Host, session: str) -> list[str]:
    """Build argv for logging into `host` and attaching `session`.

    For plain ssh we inject `-t` so the remote command gets a pty. For
    user-provided commands we assume they included `-tt` themselves. For
    local hosts we just exec tmux directly.
    """
    if host.local:
        return ["tmux", "attach", "-t", session]
    if host.command:
        return _base_argv(host) + ["tmux", "attach", "-t", session]
    return [
        "ssh",
        *SSH_OPTS,
        "-t",
        host.ssh_target,
        "tmux", "attach", "-t", session,
    ]
