from __future__ import annotations

import sys
from pathlib import Path

from . import config as cfg
from .app import TmuxmuxApp


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    path: Path | None = None
    if argv:
        path = Path(argv[0]).expanduser()
    try:
        configuration = cfg.load(path)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1
    if not configuration.hosts:
        print("No hosts defined in config.", file=sys.stderr)
        return 1
    TmuxmuxApp(configuration).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
