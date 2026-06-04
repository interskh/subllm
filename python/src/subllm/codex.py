"""CodexLLM — primary client. Wraps `codex exec` behind the BaseLLM contract."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from subllm.base import BaseLLM, _retry, _validate_with_model
from subllm.drivers.codex_exec import run_codex_exec
from subllm.errors import OutputError
from subllm.preflight import RegionGuard

_JSON_INSTRUCTION = "\n\nReturn ONLY a single JSON object. No prose, no code fence."


class CodexLLM(BaseLLM):
    def __init__(
        self,
        model: str | None = None,
        reasoning_effort: str = "medium",
        search: bool = False,
        codex_home: str | None = None,
        region_guard: RegionGuard | None = None,
        attempts: int = 3,
        timeout_s: int = 120,
    ) -> None:
        self._model = model
        self._effort = reasoning_effort
        self._search = search
        self._codex_home = codex_home
        self._guard = region_guard
        self._attempts = attempts
        self._timeout_s = timeout_s

    def _run(self, prompt: str, schema_path: str | None = None) -> str:
        if self._guard is not None:
            self._guard.check()  # RegionError before any subprocess
        return run_codex_exec(
            prompt,
            model=self._model,
            reasoning_effort=self._effort,
            search=self._search,
            codex_home=self._codex_home,
            schema_path=schema_path,
            timeout_s=self._timeout_s,
        )

    def complete(self, prompt: str) -> str:
        return _retry(lambda: self._run(prompt), attempts=self._attempts)

    def complete_json(self, prompt: str) -> dict[str, Any]:
        def _call() -> dict[str, Any]:
            return _parse_json_object(self._run(prompt + _JSON_INSTRUCTION))
        return _retry(_call, attempts=self._attempts)

    def complete_json_schema(self, prompt: str, schema_model: type) -> dict[str, Any]:
        # codex's --output-schema runs through OpenAI strict Structured-Outputs
        # validation, which rejects a vanilla model_json_schema(). Rewrite it to
        # strict form; the result is still re-validated against the ORIGINAL
        # (lenient) model below, so optional/nullable fields round-trip.
        schema = json.dumps(_to_strict_schema(schema_model.model_json_schema()))
        def _call() -> dict[str, Any]:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False
            ) as f:
                f.write(schema)
                schema_path = f.name
            try:
                parsed = _parse_json_object(self._run(prompt, schema_path=schema_path))
            finally:
                Path(schema_path).unlink(missing_ok=True)
            # Don't trust codex's binding blindly — validate against the model.
            return _validate_with_model(parsed, schema_model)
        return _retry(_call, attempts=self._attempts)


def _to_strict_schema(node: Any) -> Any:
    """Rewrite an ordinary JSON Schema in place so codex's strict
    Structured-Outputs validator accepts it: every object node gets
    `additionalProperties: false` and lists all its properties as `required`,
    and `default: null` hints (which Pydantic emits for `Optional[...] = None`)
    are dropped. Mirrors OpenAI's reference to_strict_json_schema for the shapes
    Pydantic v2 produces — nested `$defs`, `anyOf` unions, bare `$ref` (no
    sibling-`$ref` inlining, which Pydantic v2 doesn't emit). Returns `node`.
    """
    if isinstance(node, list):
        for item in node:
            _to_strict_schema(item)
        return node
    if not isinstance(node, dict):
        return node

    # A null default would tell the model to omit a field that strict mode now
    # forces it to emit. Real (non-null) defaults are tolerated and left intact.
    if "default" in node and node["default"] is None:
        del node["default"]

    props = node.get("properties")
    if isinstance(props, dict):
        node["additionalProperties"] = False
        node["required"] = list(props.keys())

    for key in ("properties", "$defs", "definitions"):
        members = node.get(key)
        if isinstance(members, dict):
            for sub in members.values():
                _to_strict_schema(sub)

    for key in ("anyOf", "allOf", "oneOf", "prefixItems", "items"):
        _to_strict_schema(node.get(key))

    return node


def _parse_json_object(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        raise OutputError(f"codex returned invalid JSON: {raw[:120]!r}") from e
    if not isinstance(parsed, dict) or not parsed:
        raise OutputError(
            f"expected non-empty JSON object, got {type(parsed).__name__}"
        )
    return parsed
