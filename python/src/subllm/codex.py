"""CodexLLM — primary client. Wraps `codex exec` behind the BaseLLM contract."""
from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path
from typing import Any, Sequence

from subllm._images import prepare_out_dir, resolve_references, save_numbered
from subllm.base import BaseLLM, ImageGenerator, ImageResult, _retry, _validate_with_model
from subllm.drivers.codex_exec import run_codex_exec
from subllm.drivers.codex_web import run_codex_web_task
from subllm.errors import ClientError, OutputError
from subllm.preflight import RegionGuard
from subllm.webagent import WebAgentResult

_JSON_INSTRUCTION = "\n\nReturn ONLY a single JSON object. No prose, no code fence."

_IMAGE_INSTRUCTION = (
    "Call the image generation tool exactly once to create the image described "
    "below. Do not answer with SVG, HTML, or code. After the tool finishes, "
    "reply with one word.\n\n"
)


class CodexLLM(BaseLLM, ImageGenerator):
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

    def generate_image(
        self, prompt: str, out_dir: str | Path, *, references: Sequence[str | Path] = (),
    ) -> ImageResult:
        """One image turn. ONE attempt, no retry: a refusal or timeout would
        re-spend ~46 s and Codex quota per attempt; the caller decides.
        `references` are image files attached to the prompt (codex `-i`), e.g.
        a character sheet the new image must match."""
        if self._guard is not None:
            self._guard.check()  # RegionError before any subprocess
        # codex ignores a missing or unreadable -i file silently and clap may
        # split -i values on ",", so every check runs before codex starts.
        refs = resolve_references(references, forbid_comma=True)
        dest = prepare_out_dir(out_dir)
        saved: list[Path] = []

        def _collect(files: list[Path]) -> None:
            if not files:
                raise OutputError("codex produced no image (the agent did not call the image tool)")
            saved.extend(save_numbered([(f, f.suffix.lower()) for f in files], dest))

        text = run_codex_exec(
            _IMAGE_INSTRUCTION + prompt,
            model=self._model,
            reasoning_effort=self._effort,
            codex_home=self._codex_home,
            timeout_s=max(self._timeout_s, 300),
            collect_images=_collect,
            images=refs,
        )
        return ImageResult(
            paths=saved, route="codex", text=text,
            conversation_url=None, served_model=None,
        )

    def run_web_task(
        self,
        prompt: str,
        schema_model: type | None = None,
        *,
        tools: str = "web",
        timeout_s: int = 300,
    ) -> WebAgentResult:
        """One isolated web-agent turn (§4.1). ONE attempt, no retry (D-4a): a
        silent re-run would return a different browsing session than the one the
        consumer timed and grades."""
        if self._guard is not None:
            self._guard.check()  # RegionError before any subprocess
        if not self._model:
            raise ClientError("web-agent mode requires an explicit model")

        schema_path: str | None = None
        if schema_model is not None:
            schema = json.dumps(_to_strict_schema(schema_model.model_json_schema()))
            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False
            ) as f:
                f.write(schema)
                schema_path = f.name
        try:
            run = run_codex_web_task(
                prompt,
                model=self._model,
                tools=tools,
                schema_path=schema_path,
                timeout_s=timeout_s,
            )
        finally:
            if schema_path:
                Path(schema_path).unlink(missing_ok=True)

        answer: str | dict[str, Any] = run.answer
        if schema_model is not None:
            # Don't trust codex's binding blindly — validate, as the text path
            # does. A schema miss is an OutputError, not a retry (D-4a).
            answer = _validate_with_model(_parse_json_object(run.answer), schema_model)

        return WebAgentResult.build(
            client="codex",
            model=self._model,
            answer=answer,
            trace=run.trace,
            fetch_events_exposed=True,  # measured: page opens appear in the rollout
            trace_complete=run.trace_complete,
            tools=tools,
            client_version=run.client_version,
            duration_ms=run.duration_ms,
        )


def _to_strict_schema(
    node: Any, _root: Any = None, _inlining: tuple[str, ...] = ()
) -> Any:
    """Rewrite an ordinary JSON Schema in place so codex's strict
    Structured-Outputs validator accepts it: every object node gets
    `additionalProperties: false` and lists all its properties as `required`,
    and `default: null` hints (which Pydantic emits for `Optional[...] = None`)
    are dropped. Mirrors OpenAI's reference to_strict_json_schema for the shapes
    Pydantic v2 produces — nested `$defs`, `anyOf` unions, bare `$ref`, and a
    `$ref` with sibling keywords (Pydantic emits `{"$ref", "description"}` for a
    described nested-model field, and strict mode rejects it): that node is
    replaced by an inlined copy of its definition with the siblings on top. A
    `$ref` to a definition already being inlined (recursion) keeps the bare
    pointer and loses its siblings, so inlining terminates. Returns `node`.
    """
    if _root is None:
        _root = node
    if isinstance(node, list):
        for item in node:
            _to_strict_schema(item, _root, _inlining)
        return node
    if not isinstance(node, dict):
        return node

    # A null default would tell the model to omit a field that strict mode now
    # forces it to emit. Real (non-null) defaults are tolerated and left intact.
    if "default" in node and node["default"] is None:
        del node["default"]

    ref = node.get("$ref")
    if isinstance(ref, str) and len(node) > 1:
        target = _resolve_local_ref(_root, ref)
        if target is not None and ref not in _inlining:
            siblings = {k: v for k, v in node.items() if k != "$ref"}
            node.clear()
            node.update({**copy.deepcopy(target), **siblings})
            _inlining = (*_inlining, ref)
        elif target is not None:
            for key in [k for k in node if k != "$ref"]:
                del node[key]

    props = node.get("properties")
    if isinstance(props, dict):
        node["additionalProperties"] = False
        node["required"] = list(props.keys())

    for key in ("properties", "$defs", "definitions"):
        members = node.get(key)
        if isinstance(members, dict):
            for name, sub in members.items():
                inlining = _inlining
                if key != "properties" and node is _root:
                    inlining = (*_inlining, f"#/{key}/{name}")
                _to_strict_schema(sub, _root, inlining)

    for key in ("anyOf", "allOf", "oneOf", "prefixItems", "items"):
        _to_strict_schema(node.get(key), _root, _inlining)

    return node


def _resolve_local_ref(root: Any, ref: str) -> dict | None:
    """The definition a `#/$defs/<name>` (or `#/definitions/<name>`) ref names."""
    parts = ref.split("/")
    if len(parts) != 3 or parts[0] != "#" or parts[1] not in ("$defs", "definitions"):
        return None
    target = root.get(parts[1], {}).get(parts[2]) if isinstance(root, dict) else None
    return target if isinstance(target, dict) else None


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
