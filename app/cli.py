"""Command line interface for dockerfile-optimizer, invoked as `stow`."""
from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app import __version__
from app.config import VALID_SEVERITIES, Config, ConfigError
from app.config import load as load_config
from app.llm import DEFAULT_MODEL, GeminiClient, LLMError, advise
from app.parser import parse
from app.refactor import refactor_dockerfile
from app.report import ADVISORY_FORMATTERS, FORMATTERS, as_json
from app.rules import SEVERITY_ORDER, analyze, rule_catalog

COMMANDS = ("analyze", "refactor", "suggest", "rules", "version")
"""Single source of truth for the subcommand names; see test_cli."""

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INPUT = 3
EXIT_FINDINGS = 1


def _read(path: str) -> str:
    """Read a Dockerfile, normalising the directory case across platforms.

    Linux raises IsADirectoryError when opening a directory; Windows raises
    PermissionError. Checking first means the caller sees the same error, and
    the same message, everywhere.
    """
    target = Path(path)
    if target.is_dir():
        raise IsADirectoryError(path)
    return target.read_text(encoding="utf-8")


def _summarise(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts = dict.fromkeys(VALID_SEVERITIES, 0)
    for finding in findings:
        counts[str(finding["severity"])] += 1
    return counts


def cmd_analyze(args: argparse.Namespace, config: Config) -> int:
    content = _read(args.path)
    disabled = set(config.disabled_rules) | {r.upper() for r in (args.disable or [])}
    findings = [f.to_dict() for f in analyze(parse(content), disabled)]

    threshold = args.fail_on or config.fail_on
    blocking = [
        f
        for f in findings
        if SEVERITY_ORDER[str(f["severity"])] >= SEVERITY_ORDER[threshold]
    ]

    result = {
        "status": "OPTIMIZATION_NEEDED" if blocking else "PASSED",
        "target": args.path,
        "fail_on": threshold,
        "total_findings": len(findings),
        "blocking_findings": len(blocking),
        "summary": _summarise(findings),
        "details": findings,
    }

    output_format = args.format or config.output_format
    print(FORMATTERS[output_format](result), end="" if output_format == "text" else "\n")
    return EXIT_FINDINGS if blocking else EXIT_OK


def cmd_refactor(args: argparse.Namespace, config: Config) -> int:
    content = _read(args.path)
    result = refactor_dockerfile(content)

    if args.check:
        changed = result["dockerfile"] != content
        print(
            as_json(
                {
                    "status": "DRIFT" if changed else "CLEAN",
                    "target": args.path,
                    "transformations": result["transformations"],
                    "skipped": result["skipped"],
                }
            )
        )
        return EXIT_FINDINGS if changed else EXIT_OK

    if args.write:
        Path(args.path).write_text(str(result["dockerfile"]), encoding="utf-8")
        print(
            as_json(
                {
                    "status": "REWRITTEN",
                    "target": args.path,
                    "transformations": result["transformations"],
                    "skipped": result["skipped"],
                }
            )
        )
        return EXIT_OK

    print(result["dockerfile"], end="")
    for note in result["skipped"]:
        print(f"# skipped -> {note}", file=sys.stderr)
    return EXIT_OK


def cmd_suggest(args: argparse.Namespace, config: Config) -> int:
    """Advisory only. Never rewrites, never changes a deterministic finding."""
    content = _read(args.path)
    doc = parse(content)
    skipped: list[str] = refactor_dockerfile(content)["skipped"]

    try:
        questions, suggestions = advise(
            content, doc, skipped, client=GeminiClient(model=args.model or DEFAULT_MODEL)
        )
    except LLMError as error:
        print(f"stow: {error}", file=sys.stderr)
        return EXIT_USAGE

    result = {
        "status": "ADVISORY",
        "target": args.path,
        "model": args.model or DEFAULT_MODEL,
        "open_questions": [q.to_dict() for q in questions],
        "suggestions": [s.to_dict() for s in suggestions],
        "applied": False,
    }
    output_format = args.format or "text"
    print(ADVISORY_FORMATTERS[output_format](result), end="" if output_format == "text" else "\n")
    return EXIT_OK


def cmd_rules(args: argparse.Namespace, config: Config) -> int:
    catalog = rule_catalog()
    if args.format == "text":
        for entry in catalog:
            print(f"{entry['severity']:<8} {entry['rule']:<22} {entry['summary']}")
    else:
        print(as_json(catalog))
    return EXIT_OK


def cmd_version(args: argparse.Namespace, config: Config) -> int:
    print(as_json({"name": "dockerfile-optimizer", "version": __version__}))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stow",
        description="Audit and refactor Dockerfiles for caching, size and security.",
    )
    parser.add_argument("--version", action="version", version=f"stow {__version__}")
    subcommands = parser.add_subparsers(dest="command")

    analyze_cmd = subcommands.add_parser("analyze", help="audit a Dockerfile")
    analyze_cmd.add_argument("path", nargs="?", default="Dockerfile")
    analyze_cmd.add_argument("--format", choices=sorted(FORMATTERS))
    analyze_cmd.add_argument(
        "--fail-on",
        choices=VALID_SEVERITIES,
        help="lowest severity that fails the run (default: medium)",
    )
    analyze_cmd.add_argument(
        "--disable", action="append", metavar="RULE", help="disable a rule; repeatable"
    )
    analyze_cmd.set_defaults(func=cmd_analyze)

    refactor_cmd = subcommands.add_parser("refactor", help="rewrite a Dockerfile")
    refactor_cmd.add_argument("path", nargs="?", default="Dockerfile")
    group = refactor_cmd.add_mutually_exclusive_group()
    group.add_argument("--write", action="store_true", help="edit the file in place")
    group.add_argument(
        "--check", action="store_true", help="exit non-zero if a rewrite would change the file"
    )
    refactor_cmd.set_defaults(func=cmd_refactor)

    suggest_cmd = subcommands.add_parser(
        "suggest", help="ask a model about the judgement calls the engine refuses"
    )
    suggest_cmd.add_argument("path", nargs="?", default="Dockerfile")
    suggest_cmd.add_argument("--format", choices=sorted(ADVISORY_FORMATTERS), default="text")
    suggest_cmd.add_argument(
        "--model", help=f"Gemini model to ask (default: {DEFAULT_MODEL})"
    )
    suggest_cmd.set_defaults(func=cmd_suggest)

    rules_cmd = subcommands.add_parser("rules", help="list the rule catalog")
    rules_cmd.add_argument("--format", choices=("json", "text"), default="json")
    rules_cmd.set_defaults(func=cmd_rules)

    version_cmd = subcommands.add_parser("version", help="print the agent version")
    version_cmd.set_defaults(func=cmd_version)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE

    try:
        config = load_config()
    except ConfigError as error:
        print(f"stow: {error}", file=sys.stderr)
        return EXIT_USAGE

    try:
        return int(args.func(args, config))
    except FileNotFoundError:
        print(f"stow: '{args.path}' not found.", file=sys.stderr)
        return EXIT_INPUT
    except IsADirectoryError:
        print(f"stow: '{args.path}' is a directory, not a Dockerfile.", file=sys.stderr)
        return EXIT_INPUT
    except PermissionError:
        print(f"stow: '{args.path}' is not readable.", file=sys.stderr)
        return EXIT_INPUT
    except BrokenPipeError:  # piping into head/less is normal usage
        return EXIT_OK
    except UnicodeDecodeError:
        print(f"stow: '{args.path}' is not UTF-8 text.", file=sys.stderr)
        return EXIT_INPUT
    except OSError as error:
        # Every case above is an OSError subclass, and enumerating them left
        # everything else to surface as a traceback: a path with characters the
        # filesystem rejects raises EINVAL on Windows, not FileNotFoundError.
        # A CLI must never show a traceback for bad input.
        print(f"stow: cannot read '{args.path}': {error.strerror or error}.", file=sys.stderr)
        return EXIT_INPUT


if __name__ == "__main__":
    sys.exit(main())
