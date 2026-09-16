"""Advisory layer: ask a model the questions the engine refuses to answer.

The deterministic core is the trustworthy part and stays untouched. This module
only ever runs when explicitly asked, only ever *adds* suggestions, and can
never alter or suppress a deterministic finding. A model answer that does not
correspond to a question we asked is discarded, so the layer cannot invent
findings of its own.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from app.parser import Dockerfile
from app.rules import INTERPOLATED_RE

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-2.0-flash"
API_KEY_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
TIMEOUT_SECONDS = 30
CONFIDENCE = ("high", "medium", "low")

Transport = Callable[[str, bytes], bytes]


class LLMError(RuntimeError):
    """Raised when the advisory layer cannot produce an answer."""


@dataclass
class Question:
    """Something the deterministic engine declined to decide."""

    kind: str
    subject: str
    detail: str
    line: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Suggestion:
    kind: str
    subject: str
    proposal: str
    rationale: str
    confidence: str = "low"
    source: str = "model"
    """Always 'model'. Deterministic findings never carry this field."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# What the engine could not decide
# --------------------------------------------------------------------------


def open_questions(doc: Dockerfile, skipped: list[str]) -> list[Question]:
    """Turn the engine's refusals into questions worth asking a model."""
    questions: list[Question] = []
    aliases = {s.alias for s in doc.stages if s.alias}

    for stage in doc.stages:
        image = stage.base
        if not image or image in aliases or image == "scratch":
            continue
        if INTERPOLATED_RE.search(image) or "@sha256:" in image:
            continue
        tag = image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else ""
        if tag and tag != "latest":
            continue
        questions.append(
            Question(
                kind="PINNED_VERSION",
                subject=image,
                detail=(
                    "The engine will not invent a replacement tag. Which specific "
                    "version tag suits this build, judging from what it installs?"
                ),
                line=stage.line,
            )
        )

    if any(note.startswith("MULTISTAGE:") for note in skipped) and len(doc.stages) == 1:
        questions.append(
            Question(
                kind="MULTISTAGE",
                subject="single-stage build",
                detail=(
                    "The engine only splits a build matching one FROM plus one "
                    "`pip install -r`. Can this build be split into a builder and a "
                    "lean runtime stage, and what would the boundary be?"
                ),
                line=doc.stages[0].line if doc.stages else 0,
            )
        )

    return questions


# --------------------------------------------------------------------------
# Prompting
# --------------------------------------------------------------------------

SYSTEM_RULES = """\
You are advising a deterministic Dockerfile analyser. It has already found and
fixed everything it can decide mechanically. You are asked ONLY about the
judgement calls it deliberately refuses to make.

Rules you must follow:
- Answer only the questions listed. Do not raise new issues.
- If you cannot answer a question with genuine confidence, set confidence to
  "low" and say plainly what you would need to know.
- Never invent a version that you are not reasonably sure exists.
- Be specific and terse. A proposal is one concrete action.

Reply with JSON only: a list of objects with keys
"kind", "subject", "proposal", "rationale", "confidence".
"confidence" must be one of "high", "medium", "low".
"""


def build_prompt(dockerfile: str, questions: list[Question]) -> str:
    listed = "\n".join(
        f"{i}. [{q.kind}] subject={q.subject!r} line={q.line} -- {q.detail}"
        for i, q in enumerate(questions, start=1)
    )
    return (
        f"{SYSTEM_RULES}\n\nDockerfile:\n```dockerfile\n{dockerfile}\n```\n\n"
        f"Questions:\n{listed}\n"
    )


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


def api_key() -> str:
    for name in API_KEY_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise LLMError(
        "no API key: set GEMINI_API_KEY (get one at https://aistudio.google.com/apikey)"
    )


def _post(url: str, payload: bytes) -> bytes:
    """Default transport. Standard library only, so the core stays dependency-free."""
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return bytes(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:400]
        raise LLMError(f"Gemini returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise LLMError(f"could not reach Gemini: {error.reason}") from error
    except TimeoutError as error:
        raise LLMError(f"Gemini timed out after {TIMEOUT_SECONDS}s") from error


@dataclass
class GeminiClient:
    model: str = DEFAULT_MODEL
    transport: Transport = _post
    key_source: Callable[[], str] = api_key
    extra: dict[str, Any] = field(default_factory=dict)

    def complete(self, prompt: str) -> str:
        payload = json.dumps(
            {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    # Advice that changes between runs is advice nobody can act on.
                    "temperature": 0,
                    **self.extra,
                },
            }
        ).encode("utf-8")
        url = f"{API_ROOT}/{self.model}:generateContent?key={self.key_source()}"
        raw = self.transport(url, payload)
        return _extract_text(raw)


def _extract_text(raw: bytes) -> str:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LLMError("Gemini response was not JSON") from error

    if "error" in body:
        message = body["error"].get("message", "unknown error")
        raise LLMError(f"Gemini error: {message}")
    try:
        return str(body["candidates"][0]["content"]["parts"][0]["text"])
    except (KeyError, IndexError, TypeError) as error:
        blocked = body.get("promptFeedback", {}).get("blockReason")
        if blocked:
            raise LLMError(f"Gemini declined to answer: {blocked}") from error
        raise LLMError("Gemini response had no candidate text") from error


# --------------------------------------------------------------------------
# Parsing and guarding the answer
# --------------------------------------------------------------------------


def parse_suggestions(text: str, questions: list[Question]) -> list[Suggestion]:
    """Accept only well-formed answers to questions we actually asked.

    The guard matters more than the parsing: without it the advisory layer could
    introduce findings the deterministic engine never made, which is exactly the
    trust boundary this design exists to hold.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise LLMError("model did not return JSON") from error

    if isinstance(payload, dict):
        payload = payload.get("suggestions", payload.get("results", [payload]))
    if not isinstance(payload, list):
        raise LLMError("model returned JSON that was not a list of suggestions")

    asked = {(q.kind, q.subject) for q in questions}
    kinds = {q.kind for q in questions}
    suggestions: list[Suggestion] = []

    for item in payload:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).strip()
        subject = str(item.get("subject", "")).strip()
        proposal = str(item.get("proposal", "")).strip()
        if not proposal or kind not in kinds:
            continue  # an answer to a question nobody asked
        if (kind, subject) not in asked:
            # Right kind, drifted subject: keep it, but pin it to what we asked.
            subject = next(q.subject for q in questions if q.kind == kind)
        confidence = str(item.get("confidence", "low")).strip().lower()
        suggestions.append(
            Suggestion(
                kind=kind,
                subject=subject,
                proposal=proposal,
                rationale=str(item.get("rationale", "")).strip(),
                confidence=confidence if confidence in CONFIDENCE else "low",
            )
        )
    return suggestions


def advise(
    dockerfile: str,
    doc: Dockerfile,
    skipped: list[str],
    client: GeminiClient | None = None,
) -> tuple[list[Question], list[Suggestion]]:
    """Ask about the engine's refusals. No questions means no API call at all."""
    questions = open_questions(doc, skipped)
    if not questions:
        return [], []
    active = client or GeminiClient()
    answer = active.complete(build_prompt(dockerfile, questions))
    return questions, parse_suggestions(answer, questions)
