"""Backwards-compatible entry point: `python -m app.main` still works."""
from __future__ import annotations

import sys
from collections.abc import Sequence

from app.cli import COMMANDS
from app.cli import main as cli_main


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]
    # `stow Dockerfile` used to mean `stow analyze Dockerfile`.
    if argv and not argv[0].startswith("-") and argv[0] not in COMMANDS:
        argv = ["analyze", *argv]
    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
