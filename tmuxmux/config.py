from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Host:
    name: str
    username: str | None = None
    # Full shell command that establishes the login session, up to and
    # including the ssh destination (NO remote command). If set, we shlex-split
    # it and append `tmux ls` / `tmux attach -t <session>` as remote args.
    # Must include `-tt` (or `-t`) because we always invoke a remote command.
    command: str | None = None
    # When true, skip ssh entirely and run tmux directly on the local machine.
    local: bool = False

    @property
    def ssh_target(self) -> str:
        return f"{self.username}@{self.name}" if self.username else self.name


@dataclass(frozen=True)
class Config:
    hosts: tuple[Host, ...]


CONFIG_SEARCH_PATHS = (
    Path.cwd() / "hosts.toml",
    Path(os.path.expanduser("~/.config/tmuxmux/hosts.toml")),
)


def find_config() -> Path:
    for p in CONFIG_SEARCH_PATHS:
        if p.is_file():
            return p
    raise FileNotFoundError(
        "No hosts.toml found. Looked in: "
        + ", ".join(str(p) for p in CONFIG_SEARCH_PATHS)
    )


def load(path: Path | None = None) -> Config:
    path = path or find_config()
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    entries = raw.get("hosts", [])
    hosts = tuple(
        Host(
            name=e["name"],
            username=e.get("username"),
            command=e.get("command"),
            local=bool(e.get("local", False)),
        )
        for e in entries
    )
    return Config(hosts=hosts)
