#!/usr/bin/env python3
"""Replay a dump through pyte the same way tmuxmux does, and snapshot the screen.

Usage:
    python tools/dump_render.py path/to/file.dump
    python tools/dump_render.py path/to/file.dump --rows 60 --cols 240
    python tools/dump_render.py path/to/file.dump --until 50000   # stop after N read bytes
    python tools/dump_render.py path/to/file.dump --styles         # also dump per-cell fg
"""
from __future__ import annotations

import argparse
import codecs
import sys
from pathlib import Path

import pyte

sys.path.insert(0, str(Path(__file__).parent))
from dump_replay import parse  # noqa: E402


def normalize_newlines(text: str, state: dict) -> str:
    out = []
    prev_cr = state.get("last_cr", False)
    for ch in text:
        if ch == "\n" and not prev_cr:
            out.append("\r\n")
            prev_cr = False
        else:
            out.append(ch)
            prev_cr = ch == "\r"
    state["last_cr"] = prev_cr
    return "".join(out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("path", type=Path)
    p.add_argument("--rows", type=int, default=60)
    p.add_argument("--cols", type=int, default=240)
    p.add_argument("--until", type=int, default=None, help="stop after N read bytes consumed")
    p.add_argument("--styles", action="store_true")
    p.add_argument("--no-normalize", action="store_true")
    args = p.parse_args()

    screen = pyte.Screen(args.cols, args.rows)
    stream = pyte.Stream(screen)
    stream.use_utf8 = False
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    state = {"last_cr": False}

    buf = args.path.read_bytes()
    consumed = 0
    for direction, payload in parse(buf):
        if direction != "R":
            continue
        if args.until is not None and consumed >= args.until:
            break
        consumed += len(payload)
        text = decoder.decode(payload)
        if not args.no_normalize:
            text = normalize_newlines(text, state)
        try:
            stream.feed(text)
        except Exception as e:
            print(f"[stream error: {e!r}]", file=sys.stderr)

    print(f"--- screen after {consumed} bytes of read (cursor at y={screen.cursor.y} x={screen.cursor.x}) ---")
    for y in range(screen.lines):
        cells = screen.buffer[y]
        line = "".join(cells[x].data if cells[x].data else " " for x in range(screen.columns))
        print(f"{y:3d} |{line.rstrip()}|")

    if args.styles:
        print("\n--- distinct foreground colors per row ---")
        for y in range(screen.lines):
            cells = screen.buffer[y]
            fgs = []
            last = None
            for x in range(screen.columns):
                fg = cells[x].fg
                if fg != last:
                    fgs.append(f"{x}:{fg}")
                    last = fg
            print(f"{y:3d} {fgs}")


if __name__ == "__main__":
    main()
