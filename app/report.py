"""Output formatters.

JSON is for machines, text is for the developer reading CI logs, and SARIF is
what GitHub code scanning ingests to put findings inline on the diff.
"""
from __future__ import annotations

import json
from typing import Any

from app import __version__
from app.rules import SEVERITY_ORDER, rule_catalog

TOOL_URI = "https://github.com/Oruwe/dockerfile-optimizer"
SARIF_LEVEL = {
    "critical": "error",
    "high": "error",
    "medium": "warning",
    "low": "note",
}
MARKER = {"critical": "✖", "high": "✖", "medium": "▲", "low": "·"}


def as_json(payload: Any) -> str:
    return json.dumps(payload, indent=2)


def as_text(result: dict[str, Any]) -> str:
    """Human-readable summary, grouped by severity, highest first."""
    findings: list[dict[str, Any]] = result.get("details", [])
    target = result.get("target", "Dockerfile")
    if not findings:
        return f"{target}: clean — no findings.\n"

    lines = [f"{target}: {len(findings)} finding(s)", ""]
    for finding in sorted(
        findings,
        key=lambda f: (-SEVERITY_ORDER[str(f["severity"])], f["stage"], f["line"]),
    ):
        severity = str(finding["severity"])
        location = f"line {finding['line']}" if finding["line"] else "file"
        lines.append(
            f"  {MARKER[severity]} {severity.upper():<8} {finding['rule']:<22}"
            f" {location} (stage {finding['stage']})"
        )
        lines.append(f"      {finding['message']}")
        if finding.get("remediation"):
            lines.append(f"      fix: {finding['remediation']}")
        lines.append("")

    counts: dict[str, int] = result.get("summary", {})
    if counts:
        tally = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
        lines.append(f"summary: {tally}")
    return "\n".join(lines) + "\n"


def as_sarif(result: dict[str, Any]) -> str:
    """SARIF 2.1.0, so findings land inline on a GitHub pull request diff."""
    findings: list[dict[str, Any]] = result.get("details", [])
    target = str(result.get("target", "Dockerfile"))

    rules = [
        {
            "id": entry["rule"],
            "name": entry["rule"],
            "shortDescription": {"text": entry["summary"]},
            "defaultConfiguration": {"level": SARIF_LEVEL[entry["severity"]]},
            "properties": {"security-severity": _security_severity(entry["severity"])},
        }
        for entry in rule_catalog()
    ]

    results = [
        {
            "ruleId": finding["rule"],
            "level": SARIF_LEVEL[str(finding["severity"])],
            "message": {"text": _message(finding)},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": target},
                        # SARIF regions are 1-based; findings about the file as a
                        # whole carry line 0, which must not be emitted verbatim.
                        "region": {"startLine": max(int(finding["line"]), 1)},
                    }
                }
            ],
        }
        for finding in findings
    ]

    return json.dumps(
        {
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "name": "dockerfile-optimizer",
                            "version": __version__,
                            "informationUri": TOOL_URI,
                            "rules": rules,
                        }
                    },
                    "results": results,
                }
            ],
        },
        indent=2,
    )


def _message(finding: dict[str, Any]) -> str:
    message = str(finding["message"])
    remediation = str(finding.get("remediation") or "")
    return f"{message} {remediation}".strip()


def _security_severity(severity: str) -> str:
    """GitHub ranks code-scanning alerts on a 0-10 scale."""
    return {"critical": "9.0", "high": "7.0", "medium": "4.0", "low": "2.0"}[severity]


CONFIDENCE_MARKER = {"high": "●", "medium": "◐", "low": "○"}


def as_advisory_text(result: dict[str, Any]) -> str:
    """Render suggestions, kept visually separate from deterministic findings.

    A reader must never have to work out which half of the output a line came
    from: one half is reproducible, the other is a model's opinion.
    """
    target = result.get("target", "Dockerfile")
    questions: list[dict[str, Any]] = result.get("open_questions", [])
    suggestions: list[dict[str, Any]] = result.get("suggestions", [])

    if not questions:
        return f"{target}: nothing the engine could not decide. No model call made.\n"

    lines = [
        f"{target}: {len(questions)} open question(s) the engine declines to answer",
        f"model: {result.get('model', 'unknown')} — suggestions below are advisory, "
        "not reproducible",
        "",
    ]
    for question in questions:
        lines.append(f"  ? {question['kind']}  {question['subject']}  (line {question['line']})")
        lines.append(f"      {question['detail']}")
        answered = [s for s in suggestions if s["kind"] == question["kind"]]
        for suggestion in answered:
            marker = CONFIDENCE_MARKER.get(str(suggestion["confidence"]), "○")
            lines.append(
                f"      {marker} model ({suggestion['confidence']}): {suggestion['proposal']}"
            )
            if suggestion.get("rationale"):
                lines.append(f"        because: {suggestion['rationale']}")
        if not answered:
            lines.append("      (model returned no usable answer)")
        lines.append("")

    lines.append("Nothing above has been applied. Verify before acting on it.")
    return "\n".join(lines) + "\n"


FORMATTERS = {"json": as_json, "text": as_text, "sarif": as_sarif}
ADVISORY_FORMATTERS = {"json": as_json, "text": as_advisory_text}
