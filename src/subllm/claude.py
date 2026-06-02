"""ClaudeLLM — fallback client. Drives interactive claude over tmux.

JSON is prompt-engineered then validated (no native schema binding on this path).
"""
from __future__ import annotations

import json
import re
from typing import Any

from subllm.base import BaseLLM, _retry, _validate_with_model
from subllm.drivers.claude_tmux import run_claude_tmux
from subllm.errors import OutputError
from subllm.preflight import RegionGuard

_JSON_INSTRUCTION = "\n\nReturn ONLY a single JSON object — no prose, no code fence."


class ClaudeLLM(BaseLLM):
    def __init__(
        self,
        model: str | None = None,
        permission_mode: str = "bypassPermissions",
        region_guard: RegionGuard | None = None,
        attempts: int = 3,
        timeout_s: int = 300,
    ) -> None:
        self._model = model
        self._permission_mode = permission_mode
        self._guard = region_guard
        self._attempts = attempts
        self._timeout_s = timeout_s

    def _run(self, prompt: str) -> str:
        if self._guard is not None:
            self._guard.check()
        return run_claude_tmux(
            prompt,
            model=self._model,
            permission_mode=self._permission_mode,
            timeout_s=self._timeout_s,
        )

    def complete(self, prompt: str) -> str:
        return _retry(lambda: self._run(prompt), attempts=self._attempts)

    def complete_json(self, prompt: str) -> dict[str, Any]:
        def _call() -> dict[str, Any]:
            return _extract_json_object(self._run(prompt + _JSON_INSTRUCTION))
        return _retry(_call, attempts=self._attempts)

    def complete_json_schema(self, prompt: str, schema_model: type) -> dict[str, Any]:
        # Claude has no native schema binding — instruct + parse + validate.
        instruction = (
            _JSON_INSTRUCTION
            + " Match this JSON schema: "
            + json.dumps(schema_model.model_json_schema())
        )
        def _call() -> dict[str, Any]:
            parsed = _extract_json_object(self._run(prompt + instruction))
            return _validate_with_model(parsed, schema_model)
        return _retry(_call, attempts=self._attempts)


def _extract_json_object(raw: str) -> dict[str, Any]:
    """Extract the first VALID top-level JSON object. Strips ```json fences, then
    scans each '{' with json.JSONDecoder().raw_decode — robust against prose
    braces and trailing text (a greedy \\{.*\\} regex would mis-grab both)."""
    text = (raw or "").strip()
    if "```" in text:
        # keep the content of the first fenced block if present
        fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed:
            return parsed
    raise OutputError(f"no JSON object in claude output: {raw[:120]!r}")
