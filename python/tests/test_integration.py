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
    SUBLLM_LIVE=1 uv run pytest -m integration -k image     # codex + chatgpt.com image (spends quota)
    SUBLLM_LIVE=1 SUBLLM_WEB=1 uv run pytest -m integration -k chatgpt_web  # chatgpt.com web route

Requirements:
    codex   -> `codex` logged in to your ChatGPT plan
    claude  -> `claude` logged in to your Pro/Max plan, plus `tmux`
    chatgpt_web -> ego lite running with a manual chatgpt.com login; also needs SUBLLM_WEB=1
"""
from __future__ import annotations

import os
import shutil
import struct
import zlib

import pytest
from pydantic import BaseModel

from subllm.chatgpt_web import ChatGPTWebLLM
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


def _assert_decodable_png(path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"not a PNG: {path.name}"
    pos, idat, size = 8, b"", None
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        assert len(body) == length, "truncated PNG chunk"
        if kind == b"IHDR":
            size = struct.unpack(">II", body[:8])
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
        if kind == b"IEND":
            break
    assert size and size[0] > 0 and size[1] > 0, f"bad IHDR: {size}"
    assert zlib.decompress(idat), "IDAT did not decompress"
    return size


@_need_codex
def test_generate_image_writes_decodable_png(tmp_path):
    # Spends a little Codex quota (~46 s per image). Proves the whole route:
    # prompt -> codex image tool -> generated_images collection -> out_dir.
    out_dir = tmp_path / "out"
    res = _live(lambda: CodexLLM().generate_image(
        "A simple red circle on a plain white background.", out_dir
    ))
    assert res.paths, "no image files returned"
    for p in res.paths:
        assert p.resolve().parent == out_dir.resolve(), f"outside out_dir: {p}"
        _assert_decodable_png(p)
    assert res.route == "codex"
    assert res.conversation_url is None and res.served_model is None


@pytest.mark.skipif(
    not os.environ.get("SUBLLM_WEB"),
    reason="chatgpt.com web route test; set SUBLLM_WEB=1 to run",
)
@pytest.mark.skipif(shutil.which("ego-browser") is None, reason="ego-browser not on PATH")
def test_chatgpt_web_complete_result_round_trips_token():
    # Spends a little ChatGPT Chat quota and leaves one regular chat in history.
    token = "zorblax"
    res = _live(lambda: ChatGPTWebLLM().complete_result(
        f"Reply with exactly this one word and nothing else: {token}"
    ))
    assert token in res.text.lower(), f"token did not round-trip: {res.text!r}"
    assert res.format in {"markdown", "plain"}
    assert res.conversation_url.startswith("https://chatgpt.com/c/")


def _assert_webp(path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP", f"not a WebP: {path.name}"
    kind, body = data[12:16], data[20:]
    if kind == b"VP8 ":
        w, h = struct.unpack("<HH", body[6:10])
        size = (w & 0x3FFF, h & 0x3FFF)
    elif kind == b"VP8L":
        bits = struct.unpack("<I", body[1:5])[0]
        size = ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    elif kind == b"VP8X":
        size = (int.from_bytes(body[4:7], "little") + 1, int.from_bytes(body[7:10], "little") + 1)
    else:
        raise AssertionError(f"unknown WebP chunk {kind!r}")
    assert size[0] > 0 and size[1] > 0, f"bad WebP size: {size}"
    return size


@pytest.mark.skipif(
    not os.environ.get("SUBLLM_WEB"),
    reason="chatgpt.com web route test; set SUBLLM_WEB=1 to run",
)
@pytest.mark.skipif(shutil.which("ego-browser") is None, reason="ego-browser not on PATH")
def test_chatgpt_web_generate_image_writes_decodable_image(tmp_path):
    # Spends a little ChatGPT Chat quota (~40 s) and leaves one regular chat in history.
    out_dir = tmp_path / "out"
    res = _live(lambda: ChatGPTWebLLM().generate_image(
        "A simple red circle on a plain white background.", out_dir
    ))
    assert res.paths, "no image files returned"
    for p in res.paths:
        assert p.resolve().parent == out_dir.resolve(), f"outside out_dir: {p}"
        head = p.read_bytes()[:12]
        size = _assert_webp(p) if head[:4] == b"RIFF" else _assert_decodable_png(p)
        assert min(size) >= 256, f"image too small: {size}"
    assert res.route == "web"
    assert res.conversation_url.startswith("https://chatgpt.com/c/")
