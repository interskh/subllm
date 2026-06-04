"""Live integration smoke tests — these call the REAL codex / claude
subscriptions. Everything else in tests/ stubs the binaries; this file is the
one place that proves the clients actually work end-to-end.

Opt-in only. Skipped unless SUBLLM_LIVE=1 is set, so CI and a normal
`uv run pytest` never touch your subscription. Each client also self-skips if
its required binaries aren't on PATH, and a QuotaError becomes a skip (not a
failure) so a rate-limited run degrades gracefully instead of going red.

Run:
    SUBLLM_LIVE=1 uv run pytest -m integration -v
    SUBLLM_LIVE=1 uv run pytest -m integration -k codex     # codex only
    SUBLLM_LIVE=1 uv run pytest -m integration -k claude    # claude only

Requirements:
    codex   -> `codex` logged in to your ChatGPT plan
    claude  -> `claude` logged in to your Pro/Max plan, plus `tmux`
"""
from __future__ import annotations

import os
import shutil

import pytest
from pydantic import BaseModel

from subllm.claude import ClaudeLLM
from subllm.codex import CodexLLM
from subllm.errors import QuotaError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("SUBLLM_LIVE"),
        reason="live subscription test; set SUBLLM_LIVE=1 to run",
    ),
]

_need_codex = pytest.mark.skipif(
    shutil.which("codex") is None, reason="codex not on PATH"
)
_need_claude = pytest.mark.skipif(
    shutil.which("claude") is None or shutil.which("tmux") is None,
    reason="claude and tmux must both be on PATH",
)

# Build the client lazily (a thunk) so collection never spawns a subprocess.
_CLIENTS = [
    pytest.param(lambda: CodexLLM(), id="codex", marks=_need_codex),
    pytest.param(lambda: ClaudeLLM(), id="claude", marks=_need_claude),
]


class _Summary(BaseModel):
    title: str
    word_count: int


def _live(call):
    """Run a real call; turn a quota / rate-limit into a skip, not a failure —
    'couldn't verify' is a different outcome from 'the code is broken'."""
    try:
        return call()
    except QuotaError as e:
        pytest.skip(f"subscription quota / rate limit hit: {e}")


@pytest.mark.parametrize("make_client", _CLIENTS)
def test_complete_round_trips_instruction(make_client):
    # The sentinel word proves our prompt reached the model AND its reply came
    # back through the driver — the whole point of a live smoke test. A test that
    # only asserted "non-empty" couldn't tell a real answer from a banner echo.
    llm = make_client()
    out = _live(lambda: llm.complete(
        "Reply with exactly this one word and nothing else: pong"
    ))
    assert out.strip(), "live completion was empty"
    assert "pong" in out.lower(), f"instruction did not round-trip: {out!r}"


@pytest.mark.parametrize("make_client", _CLIENTS)
def test_complete_json_returns_requested_object(make_client):
    # Asserts the JSON path (prompt-engineered for claude, native for codex)
    # yields a parseable object carrying the key we asked for — not just any dict.
    llm = make_client()
    out = _live(lambda: llm.complete_json(
        'Return a JSON object with a single key "answer".'
    ))
    assert isinstance(out, dict) and out, f"expected non-empty dict, got {out!r}"
    assert "answer" in out, f"requested key missing: {out!r}"


@pytest.mark.parametrize("make_client", _CLIENTS)
def test_complete_json_schema_validates_live(make_client):
    # Exercises native structured output (codex --output-schema) vs. claude's
    # instruct-and-validate path. The returned dict is already model-validated,
    # so reaching this assert means the live output satisfied _Summary.
    llm = make_client()
    out = _live(lambda: llm.complete_json_schema(
        "Summarize this text with a short title and its word count: "
        "'the quick brown fox jumps over the lazy dog'.",
        _Summary,
    ))
    assert set(out) >= {"title", "word_count"}, f"schema keys missing: {out!r}"
    assert isinstance(out["word_count"], int)
    assert out["title"].strip(), "title was empty"
