import json
import pytest
from pydantic import BaseModel
from subllm.codex import CodexLLM
from subllm.errors import OutputError, RegionError


class _Summary(BaseModel):
    topic: str


def test_complete_returns_text(fake_bin, tmp_path):
    fake_bin("codex", r'''#!/bin/bash
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
printf 'hello world' > "$out"
''')
    llm = CodexLLM(model="gpt-5.4", codex_home=str(tmp_path))
    assert llm.complete("hi") == "hello world"


def test_complete_json_parses_object(fake_bin, tmp_path):
    fake_bin("codex", r'''#!/bin/bash
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
printf '{"topic": "weekend trip"}' > "$out"
''')
    llm = CodexLLM(model="gpt-5.4", codex_home=str(tmp_path))
    assert llm.complete_json("x") == {"topic": "weekend trip"}


def test_complete_json_rejects_non_object(fake_bin, tmp_path):
    fake_bin("codex", r'''#!/bin/bash
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
printf '[]' > "$out"
''')
    llm = CodexLLM(model="gpt-5.4", codex_home=str(tmp_path), attempts=1)
    with pytest.raises(OutputError):
        llm.complete_json("x")


def test_complete_json_schema_validates(fake_bin, tmp_path):
    fake_bin("codex", r'''#!/bin/bash
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
printf '{"topic": "ok"}' > "$out"
''')
    llm = CodexLLM(model="gpt-5.4", codex_home=str(tmp_path))
    assert llm.complete_json_schema("x", _Summary) == {"topic": "ok"}


def test_complete_json_schema_rejects_mismatch(fake_bin, tmp_path):
    # codex returns a JSON object that does NOT satisfy _Summary (missing 'topic')
    fake_bin("codex", r'''#!/bin/bash
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
printf '{"wrong": "field"}' > "$out"
''')
    llm = CodexLLM(model="gpt-5.4", codex_home=str(tmp_path), attempts=1)
    with pytest.raises(OutputError):
        llm.complete_json_schema("x", _Summary)


def test_region_guard_blocks_before_calling(tmp_path):
    from subllm.preflight import RegionGuard
    guard = RegionGuard(allowed_regions={"US"}, lookup=lambda: "CN", ttl_s=0)
    # No fake codex on PATH; if the guard works, codex is never invoked.
    llm = CodexLLM(model="gpt-5.4", codex_home=str(tmp_path), region_guard=guard)
    with pytest.raises(RegionError):
        llm.complete("x")
