"""CodexLLM.complete_json_schema must accept ordinary Pydantic/JSON schemas.

`codex exec --output-schema` validates through OpenAI strict Structured-Outputs
mode, which rejects a schema straight from `model_json_schema()` (missing
`additionalProperties: false`, optional fields not in `required`). These tests
pin the strict-mode transform AND the design contract: send codex an all-required
strict schema, but re-validate the result against the *original* lenient model so
optional/nullable fields still round-trip.
"""
import json
from typing import Optional

from pydantic import BaseModel, Field

from subllm.codex import CodexLLM, _to_strict_schema


class _Tag(BaseModel):
    name: str
    weight: float = 1.0


class _Doc(BaseModel):
    title: str
    summary: Optional[str] = None       # nullable, default None
    score: int = 0                      # non-null default
    tags: list[_Tag] = Field(default_factory=list)   # nested object in array
    primary: Optional[_Tag] = None      # optional nested object


def test_strict_schema_sets_additional_properties_false_on_every_object():
    strict = _to_strict_schema(_Doc.model_json_schema())
    assert strict["additionalProperties"] is False
    assert strict["$defs"]["_Tag"]["additionalProperties"] is False


def test_strict_schema_marks_all_properties_required():
    # Strict mode forbids "absent" optionals: every property must be required,
    # even the ones the original model defaults.
    strict = _to_strict_schema(_Doc.model_json_schema())
    assert set(strict["required"]) == {"title", "summary", "score", "tags", "primary"}
    assert set(strict["$defs"]["_Tag"]["required"]) == {"name", "weight"}


def test_strict_schema_drops_none_defaults_but_keeps_real_ones():
    # A `default: null` would tell the model to omit a field that strict mode now
    # forces it to emit — drop it. Real defaults are tolerated and left intact.
    strict = _to_strict_schema(_Doc.model_json_schema())
    assert "default" not in strict["properties"]["summary"]
    assert strict["properties"]["score"]["default"] == 0
    assert strict["$defs"]["_Tag"]["properties"]["weight"]["default"] == 1.0


class _Node(BaseModel):
    name: str
    children: list["_Node"] = Field(default_factory=list)   # self-referential


def test_strict_schema_handles_recursive_models_without_infinite_loop():
    # Self-referential models put the real object in $defs and point at it with
    # $ref. The transform must strict-ify the $defs definition (recurse into
    # $defs) WITHOUT following $ref targets — doing so would never terminate.
    strict = _to_strict_schema(_Node.model_json_schema())
    node = strict["$defs"]["_Node"]
    assert node["additionalProperties"] is False
    assert set(node["required"]) == {"name", "children"}
    # the recursive reference stays a bare pointer, not an inlined copy
    assert node["properties"]["children"]["items"] == {"$ref": "#/$defs/_Node"}


def _refs_with_siblings(node, path=""):
    found = []
    if isinstance(node, dict):
        if "$ref" in node and len(node) > 1:
            found.append(path)
        for key, value in node.items():
            found += _refs_with_siblings(value, f"{path}/{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found += _refs_with_siblings(value, f"{path}/{i}")
    return found


class _Level(BaseModel):
    level: str
    reason: str


class _Verdict(BaseModel):
    headline: str
    confidence: _Level = Field(description="How confident, with a reason.")


def test_strict_schema_inlines_a_described_nested_model():
    # Pydantic v2 emits `{"$ref": ..., "description": ...}` for a described
    # nested-model field; strict mode rejects a `$ref` with any sibling keyword.
    raw = _Verdict.model_json_schema()
    assert _refs_with_siblings(raw) == ["/properties/confidence"]
    strict = _to_strict_schema(raw)
    assert _refs_with_siblings(strict) == []
    confidence = strict["properties"]["confidence"]
    assert confidence["description"] == "How confident, with a reason."
    assert confidence["additionalProperties"] is False
    assert set(confidence["required"]) == {"level", "reason"}


class _Tree(BaseModel):
    name: str
    parent: Optional["_Tree"] = None
    first: Optional["_Tree"] = Field(default=None, description="The first child.")
    only: "_Tree | None" = None
    best: "_Leaf" = Field(description="Best leaf.")


class _Leaf(BaseModel):
    tree: Optional[_Tree] = None
    owner: _Tree = Field(description="Owning tree.")


def test_strict_schema_inlining_terminates_on_recursive_described_refs():
    strict = _to_strict_schema(_Tree.model_json_schema())
    assert _refs_with_siblings(strict) == []


def test_complete_json_schema_sends_strict_schema_to_codex(fake_bin, tmp_path):
    # Capture the schema file codex actually receives, and prove the nullable
    # round-trip: codex emits null for the now-required optionals, and the
    # original lenient model still validates it.
    seen = tmp_path / "schema-seen.json"
    fake_bin("codex", rf'''#!/bin/bash
out=""; schema=""
while [ $# -gt 0 ]; do
  [ "$1" = "-o" ] && out="$2"
  [ "$1" = "--output-schema" ] && schema="$2"
  shift
done
cp "$schema" "{seen}"
printf '{{"title": "x", "summary": null, "score": 5, "tags": [], "primary": null}}' > "$out"
''')
    llm = CodexLLM(model="gpt-5.4", codex_home=str(tmp_path))
    result = llm.complete_json_schema("x", _Doc)

    # validated against the original model — nulls accepted for Optional fields
    assert result == {
        "title": "x", "summary": None, "score": 5, "tags": [], "primary": None,
    }
    # the schema that reached codex was strict-mode compliant
    sent = json.loads(seen.read_text())
    assert sent["additionalProperties"] is False
    assert set(sent["required"]) == {"title", "summary", "score", "tags", "primary"}
    assert sent["$defs"]["_Tag"]["additionalProperties"] is False
