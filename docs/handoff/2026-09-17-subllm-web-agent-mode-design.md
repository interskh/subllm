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

## T2 — Codex driver web-agent mode (D-2, D-3a, D-4, D-4b, D-4c, D-5, D-6)

### Decisions
- **Trace source is the rollout, not `codex exec --json`** (measured): the simplified stream collapses a two-URL open into one item and carries no success signal.
- **`--ephemeral` is NOT passed** (deviation from D-2's literal "ephemeral session"): it suppresses the rollout, which IS the trace. The rollout is written inside the throwaway `CODEX_HOME` and dies with it, so nothing persists outside the call.
- **`HOME` is redirected to an empty temp dir** (separate from the `-C` cwd): with the real HOME the session's developer message listed `~/.agents/skills` (measured capture3).
- **Admission record keyed on (client version, model)** — `{("0.154.0","gpt-5.6-luna")}` — because the tool set comes from the per-model catalog. Fail closed, no escape hatch; the error tells the owner to re-run the live G3 control.
- **Residual D-3a risk, accepted and neutralised at runtime:** the feature-inventory check passes while `ALL_TOOLS` still offers `apply_patch` and the five `multi_agent_v1__*` bindings (no config kills them — `var-cfgkill`). That is exactly why admission does not rely on the inventory: every use is caught by the per-script scan (D-5), and a spawned sub-agent by the >1-rollout rule.
- **`unified_exec` cannot be disabled on 0.154.0** (measured here, not in CODEX_EVIDENCE.md): it reads back `true` whatever we pass, also with `code_mode_host` off. It is the single named exception in `INEFFECTIVE_DISABLES`; every other disable must read back `false` (a strengthening of D-2's check — a silently ignored disable would reopen the shell).
- **Feature-inventory allowlist:** the 30 enabled-but-allowed features, each with its one-line "adds no callable tool" justification, live in `codex_web.ENABLED_ALLOWLIST` (kept next to the check, not duplicated here).
- **`tools="none"` is strict:** any `custom_tool_call` at all (even a benign `exec`) is a violation, plus any web item. Measured live: a plain prompt under `tools="none"` emits no tool call and returns `trace: []`, so the strict rule does not break the grader path.
- **Script scan matches `\btools\b` + require `.web__run`**, not `tools\.(\w+)`, so `tools["apply_patch"]`, destructuring and `tools?.x` cannot slip past.
- **D-4b for Codex is after-the-fact and approximate**: the fetch executes on the vendor's servers, so subllm cannot block the connection — it refuses to return an answer that involved a non-public target. Names are resolved from this Mac, later than the fetch. (Measured: the client itself refused `file://`, loopback and `127.0.0.1.nip.io`, and our loopback server logged zero requests.)
- Unresolvable hostname → violation (fail closed); IsolationError rather than ClientError so the CLI still carries the trace.

### Rejected
- **`codex exec --json` as the trace** — loses openPage classification, drops the second URL of a multi-open, no success signal.
- **`action.url` as the fetch list** — it is only the FIRST ref_id of a multi-open call; requested URLs come from the script's `open:[{ref_id:…}]` array, paired by index with `results[i]`.
- **Relying on the feature inventory as D-3a proof** — it passes while apply_patch/multi_agent are still bound.
- **Config overrides to remove those bindings** (`multi_agent_version="none"`, `tools.apply_patch=false`, `include_apply_patch_tool=false`, `experimental_supported_tools=[…]`) — none of them work (measured).
- **`--disable code_mode_host`** — removes the web tool entirely; the model then answers from memory with zero web events.
- **Extending `run_codex_exec`** — text calls must keep the full environment and their current flags; web-agent mode is a separate entry point.
- **Lenient `tools="none"`** (allow a tool-free `exec`) — the closed-JS-runtime argument is exactly the reasoning D-3a says not to trust.

### Assumptions
- `find_in_page` items carry `url` in `results[i]` like `openPage` does, and have no `open:` array (so requested URL is None). **No fixture** — check by capturing one live in-page find.
- `item_completed` item types outside {UserMessage, Reasoning, AgentMessage, Extension/web.search} are treated as possible tools → violation; a benign new item type would surface as a false isolation error (it did not in three live runs).
- The `codex features list` inventory is deterministic under the isolated env (verified: identical enabled set with `env -i` + empty CODEX_HOME).
- The enabled-feature allowlist justifications are read from feature names/stages, not from codex source.

### Deferred
- Docs (`docs/drivers-contract.md`, README) — slice 2 T2 per §5; nothing here changes the existing codex_exec contract.
- Claude web-agent mode, and any pre-connection egress enforcement (slice 2 owns D-4b's "before each connection" branch).
- Fetch response metadata (status/bytes) — §7.
- (superseded in the fix round: the real parent+child sub-agent capture is now committed and both layers are tested independently.)

### Touches
- New: `python/src/subllm/publictargets.py` (shared surface — slice 2's D-4b will import it), `python/src/subllm/drivers/codex_web.py`, `python/src/subllm/drivers/codex_trace.py`.
- Changed: `python/src/subllm/codex.py` (adds `run_web_task`; existing methods untouched).
- New tests/fixtures: `python/tests/test_codex_web.py`, `python/tests/test_integration_web_agent.py`, `python/tests/fixtures/codex-0.154.0-*.jsonl|.txt` (sanitized live captures).
- Shared-surface collision risk: `WebAgentResult.build` / `TraceEvent` (T1) are consumed, not modified.

### T2 fix round (blind-reviewer findings)
- **Identifier escapes are a violation.** ECMAScript decodes `\uXXXX` / `\u{…}` / `\xXX` inside identifiers, so `tools.\u0061pply_patch(…)` ran while `scan_script` matched nothing. Any escape sequence anywhere in an exec script is now a violation — a legitimate `tools.web__run` call never needs one.
- **Unrecognised records fail closed.** The `_TOOLISH` word heuristic is gone: classification is by explicit non-tool allowlist at every layer (record type, response_item type, event_msg type, item type), and anything else is an isolation violation. It routed codex's own `FileChange`, `WebSearch` and `Extension`/`apps` items to "incomplete". §5's "other unrecognised event → incomplete" row is now carried by evidence that provably cannot be a tool call: an unknown web `action.type`, an unpaired call, a truncated stream, zero web events.
- **Real sub-agent fixture.** `codex-0.154.0-subagent-{parent,child}.jsonl` (sanitized live pair) replace the circular two-copies-of-G1 test. The two layers are tested separately: the parent's script scan catches `multi_agent_v1__spawn_agent`, and — with `scan_script` stubbed to return nothing — the file-count rule alone catches the child, which holds no tool call of its own (asserted, so the layer cannot be hidden by the other).
- **A non-statically-determinable `open:` target is a violation**, not an incomplete trace: `{ref_id:"file:"+"///x"}` and `open: urls.map(…)` previously yielded no requested URLs and skipped the D-4b requested-target check.
- **Alternate IP spellings are refused, not resolved** (`0177.0.0.1`, `0x7f000001`, `2130706433`): getaddrinfo reads the octal form as 177.0.0.1 on macOS while the kernel dials loopback.
- The resolves-to-loopback test now uses a selective resolver, so it exercises the final-URL branch it names rather than the requested-URL branch.
- Fix-round mutation battery: 6/6 caught (escape rule, allowlist classifier, non-literal ref_id, `open:` without an array literal, numeric host, rollout-count rule against the real sub-agent fixture). Live acceptance and live G1+G3 re-run green after the tightening.
