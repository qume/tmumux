"""Lightweight file logger gated by TMUXMUX_LOG.

The TUI swallows stdout/stderr, so prints disappear. When debugging,
set TMUXMUX_LOG=/tmp/tmuxmux.log and tail it in another terminal.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from typing import IO

_fh: IO[str] | None = None
_inited = False


def _init() -> None:
    global _fh, _inited
    _inited = True
    path = os.environ.get("TMUXMUX_LOG")
    if not path:
        return
    try:
        _fh = open(path, "a", buffering=1)
    except OSError:
        _fh = None


def log(msg: str, *args: object) -> None:
    if not _inited:
        _init()
    if _fh is None:
        return
    try:
        rendered = msg.format(*args) if args else msg
    except Exception:
        rendered = f"{msg} :: args={args!r}"
    try:
        _fh.write(f"{time.strftime('%H:%M:%S')} {rendered}\n")
    except OSError:
        pass


def log_exc(prefix: str) -> None:
    if not _inited:
        _init()
    if _fh is None:
        return
    try:
        _fh.write(f"{time.strftime('%H:%M:%S')} {prefix}\n")
        traceback.print_exc(file=_fh)
    except OSError:
        pass


def install_excepthook() -> None:
    """Route uncaught exceptions to the log too (Textual eats tracebacks)."""
    if not _inited:
        _init()
    if _fh is None:
        return
    prev = sys.excepthook

    def hook(exc_type, exc, tb):
        try:
            _fh.write(f"{time.strftime('%H:%M:%S')} UNCAUGHT {exc_type.__name__}: {exc}\n")
            traceback.print_exception(exc_type, exc, tb, file=_fh)
        except OSError:
            pass
        prev(exc_type, exc, tb)

    sys.excepthook = hook
