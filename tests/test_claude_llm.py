import json
import pytest
from pydantic import BaseModel
from subllm.claude import ClaudeLLM
from subllm.errors import OutputError


class _Summary(BaseModel):
    topic: str


def _patch_driver(monkeypatch, returns):
    import subllm.claude as mod
    monkeypatch.setattr(mod, "run_claude_tmux", lambda prompt, **kw: returns)


def test_complete_returns_text(monkeypatch):
    _patch_driver(monkeypatch, "claude says hi")
    assert ClaudeLLM().complete("x") == "claude says hi"


def test_complete_json_extracts_object_from_fenced_output(monkeypatch):
    _patch_driver(monkeypatch, 'Sure!\n```json\n{"k": 1}\n```\n')
    assert ClaudeLLM().complete_json("x") == {"k": 1}


def test_complete_json_handles_prose_braces_before_object(monkeypatch):
    # A greedy \{.*\} would mis-grab from the first brace in prose. raw_decode
    # scans candidate '{' positions and returns the first VALID object.
    _patch_driver(monkeypatch, 'note: use {curly} carefully. Here: {"k": 2} done')
    assert ClaudeLLM().complete_json("x") == {"k": 2}


def test_complete_json_raises_when_no_object(monkeypatch):
    _patch_driver(monkeypatch, "no json here")
    with pytest.raises(OutputError):
        ClaudeLLM(attempts=1).complete_json("x")


def test_complete_json_schema_validates(monkeypatch):
    _patch_driver(monkeypatch, '```json\n{"topic": "weekend"}\n```')
    assert ClaudeLLM().complete_json_schema("x", _Summary) == {"topic": "weekend"}


def test_complete_json_schema_rejects_mismatch(monkeypatch):
    _patch_driver(monkeypatch, '{"wrong": "field"}')
    with pytest.raises(OutputError):
        ClaudeLLM(attempts=1).complete_json_schema("x", _Summary)
