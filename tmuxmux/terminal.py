"""PTY-backed terminal widget for Textual, using pyte for emulation.

Spawns a subprocess in a pseudo-tty, reads its output non-blockingly via the
asyncio event loop, feeds it into a pyte Screen, and renders the screen into
Textual strips. Key events from Textual are translated to the byte sequences a
real xterm-like terminal would send and written back to the master fd.
"""

from __future__ import annotations

import asyncio
import codecs
import fcntl
import os
import pty
import signal
import struct
import termios
from typing import Callable

import pyte
from rich.segment import Segment
from rich.style import Style
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget


CommandFactory = Callable[[], list[str]]


class TerminalPane(Widget, can_focus=True):
    """Embedded PTY terminal.

    `cmd_factory` is called every time the process is (re)spawned — so the
    freshest argv is used (useful if the user edits config, though right now
    we don't reload).
    """

    DEFAULT_CSS = """
    TerminalPane {
        background: black;
        color: white;
        width: 1fr;
        height: 1fr;
    }
    """

    class Exited(Message):
        """Posted when the subprocess exits."""
        def __init__(self, pane: "TerminalPane") -> None:
            super().__init__()
            self.pane = pane

    def __init__(
        self,
        cmd_factory: CommandFactory,
        *,
        id: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        super().__init__(id=id)
        self._cmd_factory = cmd_factory
        self._env_overrides = dict(env) if env else {}
        self._pid: int | None = None
        self._fd: int | None = None
        self._screen: pyte.Screen | None = None
        self._stream: pyte.Stream | None = None
        self._decoder: codecs.IncrementalDecoder | None = None
        self._alive = False
        self._ever_spawned = False

    # ------------------------------------------------------------------ lifecycle

    def on_show(self) -> None:
        """Lazy-spawn on first reveal; reconnect if the process had died.

        Focus is managed by the app — don't grab it here or arrow-key preview
        in the sidebar breaks.
        """
        if not self._alive:
            self._spawn()

    def on_unmount(self) -> None:
        self._teardown()

    # -------------------------------------------------------------------- spawn

    def _spawn(self) -> None:
        self._teardown()
        cols = max(self.size.width or 80, 2)
        rows = max(self.size.height or 24, 2)
        self._screen = pyte.Screen(cols, rows)
        # Enable Line Feed / Newline Mode so a bare `\n` from the remote acts
        # as `\r\n` (move to col 0 + down). xterm's default has this off and
        # pyte mirrors that — but ssh into a remote tmux often strips the CR,
        # leaving output offset from / overprinting the prompt line.
        self._screen.set_mode(pyte.modes.LNM)
        self._stream = pyte.Stream(self._screen)
        # pyte treats UTF-8 mode as authoritative and skips `ESC(0` charset
        # switches, which breaks tmux's box-drawing (chars come through as
        # literal `q`/`x`/`j`/…). Turn UTF-8 mode off on the stream; we decode
        # bytes to str ourselves with an incremental decoder.
        self._stream.use_utf8 = False
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        cmd = self._cmd_factory()
        pid, fd = pty.fork()
        if pid == 0:
            # Child: default to xterm-256color, but let the user override via
            # config (per-host or global env). Inner TUIs that confuse pyte
            # often render better under TERM=xterm or with NO_COLOR=1.
            os.environ["TERM"] = "xterm-256color"
            # If the user launched us from inside tmux, $TMUX is inherited —
            # and `tmux attach` refuses to nest without --force. Strip it.
            for var in ("TMUX", "TMUX_PANE"):
                os.environ.pop(var, None)
            for k, v in self._env_overrides.items():
                if v == "":
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            try:
                os.execvp(cmd[0], cmd)
            except Exception:
                os._exit(127)

        self._pid = pid
        self._fd = fd
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self._set_winsize(rows, cols)

        # pyte may want to write back (e.g. reply to device-status queries).
        def _reply(data: str) -> None:
            if self._fd is None:
                return
            try:
                os.write(self._fd, data.encode("utf-8", errors="replace"))
            except OSError:
                pass

        self._screen.write_process_input = _reply

        self._alive = True
        self._ever_spawned = True
        loop = asyncio.get_event_loop()
        loop.add_reader(fd, self._on_read)
        # Seed the screen with a status line so the user sees *something* even
        # if ssh blocks or the remote is slow. Real output from tmux will
        # clear this almost immediately.
        banner = f"starting: {' '.join(cmd)}\r\n"
        self._stream.feed(banner)
        self.refresh()

    def _teardown(self) -> None:
        if self._fd is not None:
            try:
                asyncio.get_event_loop().remove_reader(self._fd)
            except (ValueError, RuntimeError):
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        if self._pid is not None:
            try:
                os.kill(self._pid, signal.SIGHUP)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                os.waitpid(self._pid, os.WNOHANG)
            except ChildProcessError:
                pass
            self._pid = None
        self._alive = False

    def _set_winsize(self, rows: int, cols: int) -> None:
        if self._fd is None:
            return
        try:
            fcntl.ioctl(
                self._fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )
        except OSError:
            pass

    # --------------------------------------------------------------------- read

    def _on_read(self) -> None:
        if self._fd is None or self._stream is None or self._decoder is None:
            return
        try:
            data = os.read(self._fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            self._on_exited()
            return
        if not data:
            self._on_exited()
            return
        try:
            text = self._decoder.decode(data)
            if text:
                self._stream.feed(text)
        except Exception:
            # pyte is generally robust, but never crash the app on a bad byte.
            pass
        self.refresh()

    def _on_exited(self) -> None:
        if not self._alive and self._fd is None and self._pid is None:
            return
        self._teardown()
        if self._stream is not None:
            self._stream.feed(
                "\r\n[session ended — ctrl+] / ctrl+\\ to cycle back will retry]\r\n"
            )
        self.refresh()
        self.post_message(self.Exited(self))

    # ------------------------------------------------------------------- resize

    def on_resize(self, event) -> None:  # type: ignore[override]
        cols = max(event.size.width, 2)
        rows = max(event.size.height, 2)
        if self._screen is not None:
            self._screen.resize(rows, cols)
        self._set_winsize(rows, cols)
        self.refresh()

    # ------------------------------------------------------------------- render

    @staticmethod
    def _color(c: str | None):
        if c is None or c == "default":
            return None
        if len(c) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in c):
            return f"#{c}"
        # pyte names diverge from rich:
        #   brightfoo  → bright_foo
        #   brown      → yellow       (pyte's name for ANSI 3/11)
        if c.startswith("bright") and not c.startswith("bright_"):
            c = "bright_" + c[6:]
        c = c.replace("brown", "yellow")
        return c

    def _style_for(self, ch) -> Style:
        return Style(
            color=self._color(ch.fg),
            bgcolor=self._color(ch.bg),
            bold=bool(ch.bold),
            italic=bool(ch.italics),
            underline=bool(ch.underscore),
            reverse=bool(ch.reverse),
            blink=bool(ch.blink),
            strike=bool(ch.strikethrough),
        )

    def render_line(self, y: int) -> Strip:
        width = self.size.width or 1
        if self._screen is None or y >= self._screen.lines:
            return Strip.blank(width)
        line = self._screen.buffer[y]
        cursor = self._screen.cursor
        cursor_on = (
            self.has_focus
            and not cursor.hidden
            and self._alive
            and y == cursor.y
            and 0 <= cursor.x < self._screen.columns
        )
        segs: list[Segment] = []
        for x in range(self._screen.columns):
            ch = line[x]
            data = ch.data if ch.data else " "
            style = self._style_for(ch)
            if cursor_on and x == cursor.x:
                style = style + Style(reverse=True)
            segs.append(Segment(data, style))
        # If the widget is wider than the screen (shouldn't happen post-resize
        # but just in case), pad.
        if self._screen.columns < width:
            segs.append(Segment(" " * (width - self._screen.columns)))
        return Strip(segs)

    # --------------------------------------------------------------------- keys

    _SPECIAL: dict[str, bytes] = {
        "enter": b"\r",
        "tab": b"\t",
        "escape": b"\x1b",
        "backspace": b"\x7f",
        "up": b"\x1b[A",
        "down": b"\x1b[B",
        "right": b"\x1b[C",
        "left": b"\x1b[D",
        "home": b"\x1bOH",
        "end": b"\x1bOF",
        "pageup": b"\x1b[5~",
        "pagedown": b"\x1b[6~",
        "delete": b"\x1b[3~",
        "insert": b"\x1b[2~",
        "f1": b"\x1bOP", "f2": b"\x1bOQ", "f3": b"\x1bOR", "f4": b"\x1bOS",
        "f5": b"\x1b[15~", "f6": b"\x1b[17~", "f7": b"\x1b[18~",
        "f8": b"\x1b[19~", "f9": b"\x1b[20~", "f10": b"\x1b[21~",
        "f11": b"\x1b[23~", "f12": b"\x1b[24~",
    }

    def _key_to_bytes(self, event) -> bytes | None:
        key = event.key
        if key in self._SPECIAL:
            return self._SPECIAL[key]
        if key.startswith("ctrl+"):
            rest = key[5:]
            if len(rest) == 1 and rest.isalpha():
                return bytes([ord(rest.lower()) - ord("a") + 1])
            if rest == "space" or rest == "@":
                return b"\x00"
            if rest == "backslash":
                return b"\x1c"
            if rest == "underscore":
                return b"\x1f"
            if rest == "right_square_bracket":
                return b"\x1d"
            if rest == "left_square_bracket":
                return b"\x1b"
        if key.startswith("shift+") and len(key) == len("shift+") + 1:
            return key[-1].upper().encode()
        if event.character is not None:
            return event.character.encode("utf-8", errors="replace")
        return None

    async def on_key(self, event) -> None:  # type: ignore[override]
        if not self._alive or self._fd is None:
            return
        data = self._key_to_bytes(event)
        if data is None:
            return
        try:
            os.write(self._fd, data)
        except OSError:
            self._on_exited()
            return
        event.stop()
        event.prevent_default()

    async def on_paste(self, event) -> None:  # type: ignore[override]
        """Forward bracketed-paste content into the PTY.

        Textual absorbs the outer terminal's `ESC[200~ … ESC[201~` into a
        single Paste event, so `on_key` never sees it. Re-wrap and write so
        the inner shell / vim / tmux treats it as an atomic paste rather
        than a burst of keystrokes.
        """
        if not self._alive or self._fd is None:
            return
        payload = "\x1b[200~" + event.text + "\x1b[201~"
        try:
            os.write(self._fd, payload.encode("utf-8", errors="replace"))
        except OSError:
            self._on_exited()
            return
        event.stop()
        event.prevent_default()

    # ----------------------------------------------------------------- external

    @property
    def is_alive(self) -> bool:
        return self._alive

    def ensure_alive(self) -> None:
        """Respawn the process if it has exited. Safe to call when already alive."""
        if self._alive:
            return
        # on_show may have fired before size was known; spawn now.
        self._spawn()
