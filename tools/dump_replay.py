#!/usr/bin/env python3
"""Pretty-print a tmuxmux PTY dump file (see TerminalPane._open_dump).

Usage:
    python tools/dump_replay.py path/to/file.dump
    python tools/dump_replay.py path/to/file.dump --raw         # bytes only
    python tools/dump_replay.py path/to/file.dump --escape      # escape ctrl chars
    python tools/dump_replay.py path/to/file.dump --grep CSI    # only chunks containing 'CSI'
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


MAGIC = b"\x1bDUMP"


def parse(buf: bytes):
    i = 0
    # Skip ASCII header (lines starting with '#') until first MAGIC.
    first = buf.find(MAGIC)
    if first == -1:
        return
    header = buf[:first]
    if header:
        yield ("H", header)
    i = first
    while i < len(buf):
        if buf[i:i + len(MAGIC)] != MAGIC:
            # Drift recovery — skip a byte and try again.
            i += 1
            continue
        i += len(MAGIC)
        if i >= len(buf):
            return
        direction = chr(buf[i])
        i += 1
        if i + 4 > len(buf):
            return
        length = int.from_bytes(buf[i:i + 4], "big")
        i += 4
        payload = buf[i:i + length]
        i += length
        yield (direction, payload)


def escape(b: bytes) -> str:
    out = []
    for byte in b:
        if byte == 0x1b:
            out.append("ESC")
        elif byte == 0x0a:
            out.append("\\n")
        elif byte == 0x0d:
            out.append("\\r")
        elif byte == 0x07:
            out.append("\\a")
        elif byte == 0x08:
            out.append("\\b")
        elif byte == 0x09:
            out.append("\\t")
        elif byte < 0x20:
            out.append(f"\\x{byte:02x}")
        elif byte == 0x7f:
            out.append("DEL")
        elif byte < 0x80:
            out.append(chr(byte))
        else:
            out.append(f"\\x{byte:02x}")
    return "".join(out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("path", type=Path)
    p.add_argument("--raw", action="store_true", help="write raw bytes to stdout")
    p.add_argument("--escape", action="store_true", help="escape every control char")
    p.add_argument("--grep", help="only show chunks whose escaped form contains this substring")
    p.add_argument("--max-bytes", type=int, default=None, help="truncate each chunk to N bytes")
    args = p.parse_args()

    buf = args.path.read_bytes()
    n_reads = n_writes = 0
    total_read = total_written = 0
    for direction, payload in parse(buf):
        if direction == "H":
            sys.stderr.write(payload.decode("utf-8", errors="replace"))
            continue
        if direction == "R":
            n_reads += 1
            total_read += len(payload)
        elif direction == "W":
            n_writes += 1
            total_written += len(payload)
        if args.max_bytes is not None:
            payload = payload[:args.max_bytes]
        if args.raw:
            sys.stdout.buffer.write(payload)
            continue
        rendered = escape(payload) if args.escape or not args.raw else payload.decode(
            "utf-8", errors="replace"
        )
        if args.grep and args.grep not in rendered:
            continue
        sys.stdout.write(f"[{direction}:{len(payload):5d}] {rendered}\n")

    sys.stderr.write(
        f"\n--- {n_reads} reads ({total_read}B), {n_writes} writes ({total_written}B)\n"
    )


if __name__ == "__main__":
    main()
