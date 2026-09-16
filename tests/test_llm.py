"""The advisory layer is only trustworthy if its boundaries hold.

These tests never touch the network: the transport is injected.
"""
import json

import pytest

from app.llm import (
    DEFAULT_MODEL,
    GeminiClient,
    LLMError,
    Question,
    advise,
    api_key,
    build_prompt,
    open_questions,
    parse_suggestions,
)
from app.parser import parse
from app.refactor import refactor_dockerfile

UNPINNED = "FROM python:latest\nCOPY . /app\nUSER 1001\n"
PINNED = "FROM python:3.11-slim\nHEALTHCHECK NONE\nUSER 1001\n"


def fake_client(payload, record=None):
    """A client whose transport returns a canned Gemini envelope."""
    def transport(url, body):
        if record is not None:
            record.append((url, json.loads(body.decode())))
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return json.dumps(
            {"candidates": [{"content": {"parts": [{"text": text}]}}]}
        ).encode()

    return GeminiClient(transport=transport, key_source=lambda: "test-key")


# --- which questions get asked -------------------------------------------


def test_unpinned_base_becomes_a_question():
    doc = parse(UNPINNED)
    kinds = [q.kind for q in open_questions(doc, [])]
    assert "PINNED_VERSION" in kinds


def test_pinned_base_asks_nothing():
    assert open_questions(parse(PINNED), []) == []


def test_digest_and_scratch_ask_nothing():
    doc = parse("FROM alpine@sha256:abc\nFROM scratch\nUSER 1\n")
    assert open_questions(doc, []) == []


def test_stage_alias_is_not_an_unpinned_image():
    doc = parse("FROM python:3.11-slim AS b\nFROM b\nUSER 1\n")
    assert open_questions(doc, []) == []


def test_skipped_multistage_becomes_a_question():
    source = "FROM python:3.11-slim\nRUN echo hi\nUSER 1001\n"
    skipped = refactor_dockerfile(source)["skipped"]
    kinds = [q.kind for q in open_questions(parse(source), skipped)]
    assert "MULTISTAGE" in kinds


# --- no questions means no spend -----------------------------------------


def test_no_questions_makes_no_api_call():
    """A clean Dockerfile must not burn quota, or cost anything."""
    calls = []

    def exploding_transport(url, body):
        calls.append(url)
        raise AssertionError("must not call the API")

    client = GeminiClient(transport=exploding_transport, key_source=lambda: "k")
    questions, suggestions = advise(PINNED, parse(PINNED), [], client=client)
    assert (questions, suggestions, calls) == ([], [], [])


# --- the trust boundary ---------------------------------------------------


ASKED = [Question(kind="PINNED_VERSION", subject="python:latest", detail="?", line=1)]


def test_model_cannot_invent_a_new_finding():
    """The whole point of the layer: it answers, it does not accuse."""
    payload = [
        {"kind": "PINNED_VERSION", "subject": "python:latest", "proposal": "use 3.11-slim"},
        {"kind": "SECRET_IN_IMAGE", "subject": "API_KEY", "proposal": "invented finding"},
    ]
    kept = parse_suggestions(json.dumps(payload), ASKED)
    assert [s.kind for s in kept] == ["PINNED_VERSION"]


def test_drifted_subject_is_pinned_back_to_what_was_asked():
    payload = [{"kind": "PINNED_VERSION", "subject": "node:latest", "proposal": "x"}]
    assert parse_suggestions(json.dumps(payload), ASKED)[0].subject == "python:latest"


def test_suggestion_without_a_proposal_is_dropped():
    payload = [{"kind": "PINNED_VERSION", "subject": "python:latest", "proposal": "  "}]
    assert parse_suggestions(json.dumps(payload), ASKED) == []


def test_unknown_confidence_degrades_to_low():
    payload = [{"kind": "PINNED_VERSION", "subject": "python:latest",
                "proposal": "x", "confidence": "absolutely certain"}]
    assert parse_suggestions(json.dumps(payload), ASKED)[0].confidence == "low"


def test_suggestions_are_labelled_as_model_output():
    payload = [{"kind": "PINNED_VERSION", "subject": "python:latest", "proposal": "x"}]
    assert parse_suggestions(json.dumps(payload), ASKED)[0].source == "model"


def test_object_wrapper_is_unwrapped():
    payload = {"suggestions": [
        {"kind": "PINNED_VERSION", "subject": "python:latest", "proposal": "x"}
    ]}
    assert len(parse_suggestions(json.dumps(payload), ASKED)) == 1


def test_non_json_answer_is_an_error_not_a_crash():
    with pytest.raises(LLMError, match="did not return JSON"):
        parse_suggestions("I think you should use python:3.11", ASKED)


def test_garbage_list_items_are_skipped():
    payload = ["a string", 42, None,
               {"kind": "PINNED_VERSION", "subject": "python:latest", "proposal": "ok"}]
    assert len(parse_suggestions(json.dumps(payload), ASKED)) == 1


# --- transport behaviour --------------------------------------------------


def test_request_targets_the_configured_model_at_temperature_zero():
    record = []
    client = fake_client([], record)
    client.model = "gemini-test"
    client.complete("hello")
    url, body = record[0]
    assert "gemini-test:generateContent" in url
    assert body["generationConfig"]["temperature"] == 0
    assert body["generationConfig"]["responseMimeType"] == "application/json"


def test_api_error_payload_becomes_a_clean_message():
    def transport(url, body):
        return json.dumps({"error": {"message": "API key not valid"}}).encode()

    client = GeminiClient(transport=transport, key_source=lambda: "bad")
    with pytest.raises(LLMError, match="API key not valid"):
        client.complete("hi")


def test_blocked_prompt_is_reported_plainly():
    def transport(url, body):
        return json.dumps({"promptFeedback": {"blockReason": "SAFETY"}}).encode()

    client = GeminiClient(transport=transport, key_source=lambda: "k")
    with pytest.raises(LLMError, match="SAFETY"):
        client.complete("hi")


def test_missing_api_key_names_the_variable(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        api_key()


def test_google_api_key_is_accepted_too(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "from-google-var")
    assert api_key() == "from-google-var"


def test_prompt_carries_the_dockerfile_and_the_questions():
    prompt = build_prompt(UNPINNED, ASKED)
    assert "FROM python:latest" in prompt
    assert "PINNED_VERSION" in prompt
    assert "Do not raise new issues" in prompt


def test_default_model_is_a_free_tier_flash_model():
    assert "flash" in DEFAULT_MODEL
