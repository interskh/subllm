# Handoff — subllm web-agent mode

Design doc: `docs/superpowers/specs/2026-09-17-subllm-web-agent-mode-design.md`. Slice 1: Codex web agent.

## T1 — shared trace types, IsolationError, CLI subcommand

### Decisions
- `TraceEvent` field order is `kind, raw_type, raw` then the optional ones; spec names/types unchanged (§4.1 listed required fields after defaulted ones, which Python rejects).
- Invariants live in `WebAgentResult.__post_init__` (ValueError), not only in `build()`: direct construction is validated too, so no code path can produce an inconsistent result. `build()` derives `retrieved_urls` and is the intended constructor.
- `derive_retrieved_urls` counts a fetch only when `ok is True` **and** `final_url is not None`; `ok=None` (unknown) is not retrieved.
- Serialization is kind-driven: always `kind, raw_type, raw`; `+query, result_urls` for search; `+url, final_url, ok` for fetch; nothing extra for `other`. `final_url` is emitted as `null` when unknown (D-4 "marked so"), never omitted.
- `--schema FILE` is adapted with `schema_model_from_json_schema()`, a pydantic `RootModel[dict]` subclass whose `model_json_schema()` returns the file's document. Keeps the `schema_model: type` signature, real `ValidationError` semantics for `_validate_with_model`, and `answer` a dict.
- `web-agent` is dispatched before the shared `except SubllmError` handler and owns its stdout error JSON; `complete`/`complete-json` keep stderr errors.
- `--model` is required for `web-agent` (unbracketed in §4.2), unlike the older subcommands.
- `--timeout` is forwarded only when given, so the client's own default stays authoritative (spec names no CLI default).
- The error JSON's `trace` is non-null **only** for `IsolationError`, keyed on the type, not on the attribute: T2 may attach a `.trace` to a diagnostic `ClientError` without widening §4.2.
- A missing/invalid `--schema` file exits 2 with a stderr message: the §4.2 error enum is closed, so this stays a usage error.

### Rejected
- Emitting every `TraceEvent` field with nulls in the JSON — a superset of §4.2's example; kind-driven keys match the contract the consumer was reviewed against.
- A duck-typed schema shim instead of `RootModel` — its `ValueError` would escape `_validate_with_model`'s `except ValidationError` in `base.py` as a traceback instead of `OutputError`.
- Passing the raw JSON-Schema dict through a new `run_web_task` parameter — new spec surface beyond §4.1's signature.
- Validating `retrieved_urls` only in `build()` — a caller could still hand-build an inconsistent frozen result; the task asks for by-construction enforcement.
- Adding `IsolationError` to `FallbackLLM`'s default `on` tuple — D-5 hard stop; a test pins that it propagates.

### Assumptions
- `schema_model_from_json_schema` does no client-side content validation: enforcement is the client's structured-output binding (same as the existing codex `--output-schema` path). Check by giving T2 a schema and a violating answer — it should fail at the client, not in subllm.
- The consumer treats `trace: []` on an isolation error as valid (non-null means isolation); `null` for every other type.
- `ClaudeLLM.__init__` stays side-effect-free, so the real-factory `--client claude` test is hermetic.

### Deferred
- `run_web_task` itself (T2 for Codex, slice 2 for Claude); the CLI raises a typed `client` error when it is absent.
- No `ok=False`/non-public-target classification logic here — that is the driver's parser (T2).
- Docs (README/drivers contract) are slice 2 T2 per §5.

### Touches
- `python/src/subllm/webagent.py` (new; shared surface — slice 2 reuses these types).
- `python/src/subllm/errors.py` (`IsolationError`), `python/src/subllm/__init__.py` (public exports: `IsolationError`, `TraceEvent`, `WebAgentResult`).
- `python/src/subllm/cli.py` (new `web-agent` subparser + `_build_web_agent_client`, the monkeypatch seam tests use).
- `python/tests/test_webagent_types.py`, `python/tests/test_cli_web_agent.py` (new), `python/tests/test_fallback.py` (one added test).

### T1 fix round (reviewer findings)
- **Total stdout contract.** `_run_web_agent` now serializes inside the try and has a catch-all `except Exception` that emits `{"error":"client", ...}` (closed enum — an internal failure is reported as a client failure) with a non-zero exit; `_emit_web_agent_error` re-dumps a minimal payload if the first dump fails. Nothing on the `web-agent` path can leave stdout empty.
- **Serializer made total, not just wrapped.** `_json_safe` coerces `raw` (and `answer`) recursively: bytes decode, everything exotic becomes `str(...)`. `IsolationError.__init__` now rejects a non-`TraceEvent` element with `TypeError`, so `.trace` really is a tuple of `TraceEvent`.
- **`--schema` must be a JSON object** (`42` is valid JSON but not a schema document) → the existing usage-error branch, exit 2/stderr. `complete-json`'s pre-existing gap left alone.
- **`__post_init__` owns its sequences:** `trace` / `retrieved_urls` coerced to tuples before validating (a caller's list could be mutated afterwards), `duration_ms` coerced to `int`.
- **An empty `final_url` is not retrieved** (`not ev.final_url`, not `is None`).
- Mutation check on the new guards: 6/6 caught (raw coercion, catch-all, schema object check, tuple coercion, empty-final_url, IsolationError element check).

### T1 second fix round (blocker re-check)
- **Trace serialization moved INSIDE the guard.** `_emit_web_agent_error(name, message, events)` now takes the unserialized `TraceEvent`s and renders them under its own try; the previous code rendered them in the `except SubllmError` clause, where a sibling `except Exception` cannot catch (a recursive or unprintable `raw` escaped with empty stdout on the D-5 path). On failure the trace degrades to `null`, never the document; the `"isolation"` type survives.
- **Write is guarded.** `_write_json_line` encodes first (no torn document) and falls back to a replace-encode; `_safe_str` covers an exception whose `__str__` raises.
- **Strict JSON.** `json_safe` (renamed from `_json_safe`, now shared with the CLI) strips lone surrogates from every string and key, and maps non-finite floats to `"nan"`/`"inf"`; `_json_document` dumps with `allow_nan=False` so a future miss fails into the guarded path instead of printing bare `NaN`.
- Mutation check: P1 in-handler serialization, P2 write fallback, P3 surrogate cleaning (both layers), P5 non-finite coercion, P6 `_safe_str`, P4b coercion+`allow_nan` pair — all caught. Single-layer removals (surrogate cleaning in `json_safe` alone; `allow_nan=False` alone) survive **by design**: they are redundant with the write guard / the coercion, and the combined mutants are caught.
