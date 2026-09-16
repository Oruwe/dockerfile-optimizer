import json
import os
from pathlib import Path

import pytest

from app.cli import EXIT_FINDINGS, EXIT_INPUT, EXIT_OK, EXIT_USAGE, main

DIRTY = "FROM python:latest\nRUN apt-get update && apt-get install -y curl\nCOPY . /app\n"
CLEAN = """FROM python:3.11-slim
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app
HEALTHCHECK NONE
USER 1001
"""


@pytest.fixture()
def dockerfile(tmp_path):
    def _write(content: str, name: str = "Dockerfile"):
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        return str(path)

    return _write


def test_analyze_exits_nonzero_on_blocking_findings(dockerfile, capsys):
    assert main(["analyze", dockerfile(DIRTY)]) == EXIT_FINDINGS
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "OPTIMIZATION_NEEDED"
    assert payload["blocking_findings"] > 0


def test_analyze_exits_zero_on_clean_file(dockerfile, capsys):
    assert main(["analyze", dockerfile(CLEAN)]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["status"] == "PASSED"


def test_fail_on_threshold_changes_the_gate(dockerfile, capsys):
    """Low-severity findings must not fail a build unless asked to."""
    path = dockerfile("FROM python:3.11-slim\nUSER 1001\n")  # only MISSING_HEALTHCHECK
    assert main(["analyze", path]) == EXIT_OK
    capsys.readouterr()
    assert main(["analyze", path, "--fail-on", "low"]) == EXIT_FINDINGS


def test_disable_flag_suppresses_a_rule(dockerfile, capsys):
    path = dockerfile(DIRTY)
    main(["analyze", path, "--disable", "PINNED_VERSION"])
    rules = [f["rule"] for f in json.loads(capsys.readouterr().out)["details"]]
    assert "PINNED_VERSION" not in rules


def test_missing_file_reports_cleanly(dockerfile, capsys):
    assert main(["analyze", "/nonexistent/Dockerfile"]) == EXIT_INPUT
    assert "not found" in capsys.readouterr().err


def test_directory_argument_reports_cleanly(tmp_path, capsys):
    """Same message on every platform.

    Linux raises IsADirectoryError opening a directory, Windows raises
    PermissionError, so relying on the OS's choice gave Windows users a
    misleading "not readable".
    """
    assert main(["analyze", str(tmp_path)]) == EXIT_INPUT
    assert "not a Dockerfile" in capsys.readouterr().err


def test_directory_argument_reports_cleanly_for_refactor(tmp_path, capsys):
    assert main(["refactor", str(tmp_path)]) == EXIT_INPUT
    assert "not a Dockerfile" in capsys.readouterr().err


def test_no_subcommand_prints_help(capsys):
    assert main([]) == EXIT_USAGE
    assert "usage" in capsys.readouterr().out.lower()


def test_sarif_output_is_valid_and_has_no_zero_line(dockerfile, capsys):
    """SARIF regions are 1-based; a file-level finding must not emit line 0."""
    main(["analyze", dockerfile(DIRTY), "--format", "sarif"])
    sarif = json.loads(capsys.readouterr().out)
    assert sarif["version"] == "2.1.0"
    run = sarif["runs"][0]
    assert run["tool"]["driver"]["name"] == "dockerfile-optimizer"
    for result in run["results"]:
        region = result["locations"][0]["physicalLocation"]["region"]
        assert region["startLine"] >= 1
    reported = {r["ruleId"] for r in run["results"]}
    declared = {r["id"] for r in run["tool"]["driver"]["rules"]}
    assert reported <= declared


def test_text_output_is_human_readable(dockerfile, capsys):
    main(["analyze", dockerfile(DIRTY), "--format", "text"])
    out = capsys.readouterr().out
    assert "CRITICAL" in out
    assert "fix:" in out


def test_refactor_check_reports_drift_without_writing(dockerfile, capsys):
    path = dockerfile(DIRTY)
    assert main(["refactor", path, "--check"]) == EXIT_FINDINGS
    assert json.loads(capsys.readouterr().out)["status"] == "DRIFT"
    assert Path(path).read_text(encoding="utf-8") == DIRTY  # unchanged


def test_refactor_check_is_clean_after_write(dockerfile, capsys):
    path = dockerfile(DIRTY)
    assert main(["refactor", path, "--write"]) == EXIT_OK
    capsys.readouterr()
    assert main(["refactor", path, "--check"]) == EXIT_OK


def test_refactor_write_makes_the_file_pass_analyze(dockerfile, capsys):
    path = dockerfile("FROM python:3.11-slim\nCOPY . /app\nRUN pip install -r requirements.txt\n")
    main(["refactor", path, "--write"])
    capsys.readouterr()
    assert main(["analyze", path]) == EXIT_OK


def test_write_and_check_are_mutually_exclusive(dockerfile):
    with pytest.raises(SystemExit):
        main(["refactor", dockerfile(DIRTY), "--write", "--check"])


def test_rules_catalog_lists_severities(capsys):
    assert main(["rules"]) == EXIT_OK
    catalog = json.loads(capsys.readouterr().out)
    assert {"rule", "severity", "summary"} <= set(catalog[0])


def test_legacy_positional_invocation_still_analyzes(dockerfile, capsys):
    """`stow Dockerfile` predates subcommands and must keep working."""
    from app.main import main as legacy_main

    assert legacy_main([dockerfile(CLEAN)]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["status"] == "PASSED"


def test_unreadable_path_reports_cleanly_not_a_traceback(tmp_path, capsys, monkeypatch):
    """Any OSError must become a message, never a traceback.

    A path containing characters the filesystem rejects raises EINVAL on
    Windows rather than FileNotFoundError, and enumerating only the familiar
    subclasses let everything else crash.
    """
    def explode(*args, **kwargs):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(Path, "read_text", explode)
    assert main(["analyze", str(tmp_path / "Dockerfile")]) == EXIT_INPUT
    err = capsys.readouterr().err
    assert "cannot read" in err
    assert "Invalid argument" in err
    assert "Traceback" not in err


def test_non_utf8_file_reports_cleanly(tmp_path, capsys):
    """Pointing the tool at a binary file must not raise UnicodeDecodeError."""
    binary = tmp_path / "Dockerfile"
    binary.write_bytes(b"\xff\xfe\x00\x01 not text at all")
    assert main(["analyze", str(binary)]) == EXIT_INPUT
    assert "not UTF-8 text" in capsys.readouterr().err


@pytest.mark.skipif(os.name != "nt", reason="only Windows rejects these characters")
def test_windows_illegal_path_characters_report_cleanly(capsys):
    assert main(["analyze", r"C:\Users\nobody\<placeholder>\Dockerfile"]) == EXIT_INPUT
    assert "Traceback" not in capsys.readouterr().err


# --- the advisory layer must stay strictly opt-in -------------------------


@pytest.fixture()
def no_network(monkeypatch):
    """Make any outbound HTTP call an immediate, loud failure."""
    def forbidden(*args, **kwargs):
        raise AssertionError("this code path must not touch the network")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)


def test_analyze_never_makes_a_network_call(dockerfile, capsys, no_network):
    """A CI gate must work offline, and must not leak a Dockerfile anywhere."""
    assert main(["analyze", dockerfile(DIRTY)]) == EXIT_FINDINGS


def test_refactor_never_makes_a_network_call(dockerfile, capsys, no_network):
    assert main(["refactor", dockerfile(DIRTY)]) == EXIT_OK


def test_rules_and_version_never_make_a_network_call(capsys, no_network):
    assert main(["rules"]) == EXIT_OK
    assert main(["version"]) == EXIT_OK


def test_suggest_without_an_api_key_fails_cleanly(dockerfile, capsys, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert main(["suggest", dockerfile(DIRTY)]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "GEMINI_API_KEY" in err
    assert "Traceback" not in err


def test_suggest_on_a_clean_file_needs_no_key_and_no_call(
    dockerfile, capsys, monkeypatch, no_network
):
    """Nothing to ask means nothing to pay for."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert main(["suggest", dockerfile(CLEAN)]) == EXIT_OK
    assert "No model call made" in capsys.readouterr().out


def test_legacy_shim_knows_every_subcommand():
    """Two hardcoded lists of subcommands is one too many."""
    import argparse

    from app.cli import COMMANDS, build_parser

    parser = build_parser()
    declared = frozenset(
        name
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
        for name in action.choices
    )
    assert declared == frozenset(COMMANDS)


def test_legacy_shim_does_not_swallow_the_suggest_command(
    dockerfile, capsys, monkeypatch, no_network
):
    from app.main import main as legacy_main

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert legacy_main(["suggest", dockerfile(CLEAN)]) == EXIT_OK
    # routed to suggest, not rewritten into `analyze suggest ...`
    assert "No model call made" in capsys.readouterr().out
