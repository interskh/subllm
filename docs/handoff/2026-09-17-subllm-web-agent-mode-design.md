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

### T2 whole-run gate fix round (gate reviewer + Codex cross-model reviewer)
- **An unreported `open:` request is now a trace event.** Leftover requested targets (the client never returned a matching web item) become `fetch` events with `ok=None`, so they run through the D-4b check instead of being dropped; a private one is a violation, a public one leaves the trace incomplete. Previously `pending_opens` was never inspected after the loop and a requested `http://10.0.0.5/…` vanished.
- **Both halves of the admitted key are cross-checked.** `turn_context.model` is carried on `ParsedRollout`; a mismatch with the requested model is a `ClientError` (a server-side alias would run an unadmitted per-model catalog), and its absence marks the trace incomplete. This mirrors the existing `cli_version` cross-check.
- **`turn_diff` is a violation**, not a known event: it is positive evidence of a file write.
- **The script scan is an allowlist of shape, not a blacklist of spellings.** A blacklist is bypassable through the prototype chain (`tools.web__run.constructor("return tools")().apply_patch(…)` names nothing forbidden). Every exec script must now consist of allowed globals (`tools`, `text`, `JSON`), allowed properties (`web__run`, `stringify`), keywords, object keys and local bindings; `constructor`, `__proto__`, `this`, `Reflect`, `Proxy`, `String`, computed member access and template literals are violations. **The shape rule as stated here was too tight to run real client scripts and is bounded in the second pass below — read that bullet for the rule in force.** Grounded in the captures: every legitimate script in the committed fixtures scans clean, and the violating ones are still flagged for the BINDING they used (`test_every_committed_capture_script_scans_as_the_client_wrote_it`).
- **A `ref_id` must be one complete string literal.** `"https://example.com"+"@127.0.0.1/"` previously read as its public prefix; the regression test now uses that public-prefix form so it fails when the rule is removed.
- **`find:` targets are extracted too, and an undeterminable fetch target is a violation.** A find whose target cannot be read no longer becomes `(url=None, final_url=None)` with a complete trace.
- **A non-URL `ref_id` resolves against earlier results** (`turn0search0` → its URL), so the ordinary search-then-open flow works and D-4b validates the resolved URL. Without this the flow raised a spurious IsolationError — a usability blocker, not a leak.
- **CLI `--schema` file errors emit the typed JSON document on stdout** (`"error":"client"`, non-zero exit): the call never reached the model, and §4.2 promises the consumer never parses stderr. **Argparse-level errors (unknown flag, bad choice) stay argparse's rc 2 on stderr** — they are argparse's contract, not ours.
- **The last-resort stdout write is genuinely encodable:** on an ASCII stdout the document is re-dumped with `ensure_ascii=True` (still strict JSON) instead of re-encoding the same Unicode as UTF-8, which raised.
- **The live G3 leak assertions can now fail:** a violating call's paired `custom_tool_call_output` is carried in the trace (that is where read content would appear), and the test asserts the blob is non-vacuous before asserting the canary/sentinel/loopback body are absent.
- **SUPERSEDED in the second pass below — search `result_urls` ARE now run through the D-4b check.** As written in this round they deliberately were not (raised by the gate reviewer; left for the orchestrator's call). The reasoning recorded here was overruled by D-4b bullet 4; it is kept only to show what was weighed. Today only fetch events are classified. Cost of changing it: search results are URLs the vendor's index returned, not addresses anything connected to from here, so a private-looking result would fail a run that never touched it — and a run whose search hits a `10.x` intranet result would become an IsolationError with no fetch. The D-4 rule that search results are never retrieved URLs already keeps them out of grounding. If the owner wants it, the cheap form is to classify `result_urls` and mark the trace incomplete (not violation) when one is non-public.
- **Not fixed, by design (Codex):** the hosted fetch's real network destinations and redirect hops are invisible to us — we re-resolve names later on this Mac. That is the spec's own position (§2 makes no egress claim; D-4b requires pre-connection enforcement only where the fetch may execute locally), so it stays a documented limitation of the Codex path.
- Gate-round mutation battery: 10/10 caught (leftover open, model cross-check, `turn_diff`, shape allowlist, one-complete-literal ref_id, undeterminable target, handle resolution, schema strict rewrite, CLI schema error channel, ASCII write fallback). One test was rewritten first because it passed for the wrong reason: the shape snippet is now appended to the capture's own legitimate `open:` call, so only the shape rule can fail the run.

### T2 gate round, second pass (shape widening + search targets)
- **The shape allowlist was over-tight and is now bounded, not removed.** As first written it admitted only `text(r)` / `text(JSON.stringify(r))`: `r.results`, `r.results[0].url`, a `for…of` over results, `.map(…).join(…)` and template literals all raised IsolationError on legitimate code, and the three captures scanned clean only because the model happened to write `text(r)`. **Rationale for widening:** reading properties of the value the ALLOWED `tools.web__run` call returned is not a capability escalation — that value is untrusted web content the script can only hand to `text()`. What now holds the line: a property DENYLIST applied to any value (`constructor`, `prototype`, `__proto__`, `call`, `apply`, `bind`, `caller`, `arguments`, the `__define*`/`__lookup*` accessors), the computed-member-access ban (a numeric index is allowed — it cannot spell a name — but `a[b]` and `a["cons"+"tructor"]` are violations), `tools` usable only as `tools.web__run(` (so no bare `const f = tools.web__run`), the global-identifier allowlist and the escape-sequence rule.
- **Local-binding tracking was broken and is fixed:** `const`/`let`/`var` including destructuring, arrow and function parameters, `for…of` / `for…in` bindings and catch params all count as declared. Previously `for (const x of r.results)` reported `x` as an unknown identifier.
- **Template literals are allowed in ordinary code, and their `${…}` expressions are SCANNED** (blanking them would have hidden `${r.constructor}`). A `ref_id`/`find` value must still be one complete string literal, so a template literal there stays a violation.
- **Search `result_urls` are held to the D-4b check (slice orchestrator's ruling this round, not re-argued here).** D-4b bullet 4 says hosted search "is held to the same after-the-fact check", and spec-review is complete (§8), so it is decided. Same classifier, same resolution-failure behaviour, same severity: a non-public search result is an `IsolationError` naming the search event and its query. **Why not "mark incomplete":** `trace_complete=false` asserts that events may be MISSING and the trace cannot be grounded on; a search that surfaced a private URL is a COMPLETE trace containing a disallowed target, and overloading the flag would degrade the one signal a consumer uses to decide whether to ground an answer.
- **Accepted as a documented limitation, not a defect (slice orchestrator's ruling):** the hosted fetch's actual network destinations and intermediate redirect hops are unchecked — subllm re-resolves names later, on this Mac. §2 makes no egress claim and D-4b's before-each-connection enforcement is scoped to clients whose fetch executes locally, which Codex's hosted fetch is not; slice 2 owns that branch. No reader should take the Codex path's D-4b check for pre-connection enforcement.
- Round-4 mutation battery: 6/6 caught (property denylist, computed-access ban, `tools`-must-be-called, template `${…}` scanning, search-side D-4b check, local-binding tracking). H3 needed a re-run with a quoted `-k` expression — the first pass reported "no tests ran", which is not a kill.

### T2 final round (prototype-chain laundering + search resolution)
- **Two reported bypasses of the widened scanner are closed.** `const {constructor: C} = async function(){}` and the `getOwnPropertyDescriptor(getPrototypeOf(async function(){}), "constructor")` path both scanned CLEAN and returned an answer. Four laundering paths, all now shut: (1) **denied names are a violation in EVERY position** — property, declared or destructured name, key, string literal, computed key, template expression — scanned on the RAW script before any string/comment blanking; (2) **key position is an allowlist** (`open`, `find`, `ref_id`, `search_query`, `q`, `response_length`, read off the committed captures), which kills `1 ? Object : 0` being read as key shape; (3) **`function` is banned outright** (arrow functions stay) as the cheapest constructor-bearing value; (4) **two string literals joined by `+` are non-determinable**, consistent with the ref_id rule, plus a computed property key (`{["x"]: v}`) is a violation.
- **Ordinary code still passes**: every shape from the widening round (`r.results`, `r.results[0].url`, `for…of`, `.map(…).join(…)`, template literals, `filter/map/join`, try/catch) scans clean, and `test_the_committed_capture_scripts_still_scan_as_before` asserts every legitimate web script in the fixtures is clean. Unknown globals `Object`, `Reflect`, `Proxy`, `Function`, `globalThis`, `String`, `Array` are all violations now that key position no longer launders them.
- **Amendment to the previous round's "same resolution-failure behaviour" instruction (the reviewer's, on evidence):** for SEARCH results only, a host that simply fails to resolve is NOT a violation — no connection was made, and D-4b judges where a connection actually goes, so an NXDOMAIN result would otherwise withhold a legitimate answer. Anything non-public BY INSPECTION (private/loopback/link-local literal, non-http(s) scheme, a name that resolves to a non-public address) is still an `IsolationError`. The FETCH path is untouched and still fails closed on an unresolvable name; `test_a_fetch_target_that_does_not_resolve_still_fails_closed` pins that split, and mutant I7 (leaking the carve-out into the fetch path) is caught.
- **Known cost of the any-position rule, accepted:** a denied name inside a URL a page legitimately uses (`https://developer.mozilla.org/…/Function/prototype/call`) makes the run fail closed with a clear isolation message. There is no position in a client-written script where these names are needed, and a false failure is recoverable (re-run) where a false pass is not — but a consumer seeing repeated `denied name 'call'` failures on documentation-heavy tasks should read it as this rule, not as a real escalation.
- **Scope note on the approach:** this is a regex/token scanner, not a JS parser. It is sound for the shapes the client emits and for every bypass found so far, but a determined bypass class remains possible in principle; the layers that do not depend on it — the D-3a admitted (version, model) record, the one-rollout rule, the D-4b target checks, the read-only sandbox and the environment allowlist — are what make a scanner miss survivable. If a third bypass class appears, the honest fix is a real JS parse (or refusing any script that is not the exact `await tools.web__run({…}); text(…)` shape), not another pattern.
- Final-round mutation battery: 7/7 caught (any-position denied names, key allowlist, `function` ban, literal concatenation, computed property key, search carve-out removed, carve-out leaking into the fetch path). Three mutants initially survived because their cases were caught by a sibling rule; a dedicated case per rule was added (`text((function(){ return 1 })())`, `text("a"+"b")`, `const {[0]: v} = r`) so each rule is load-bearing on its own.

### T2 closing item (the any-position name rule was too broad for real URLs)
- **An allowlisted key's own string VALUE is DATA and is exempt from every name-based rule** (`ref_id:"…"`, `q:"…"`, `response_length:"…"`). The previous round's any-position rule was a defect, not a conservative default: `https://developer.mozilla.org/…/Function/prototype/call`, `/docs/how-to-call-the-api` and `/apply` are ordinary URLs, and raising an isolation alarm on them trains the owner to switch the control off. Those values cannot be code — the ref_id rule already requires each to be one complete string literal — and each is checked downstream as a fetch or search target anyway.
- **The exemption is bounded to that one position.** A string literal ANYWHERE else — a call argument, an array element, a non-allowlisted key's value — is still scanned, which is what keeps the reported `getOwnPropertyDescriptor(…, "constructor")` bypass caught. Masking is applied to all four name rules (denied names, `\u`/`\x` escapes, the `tools` reference check and the runtime-escape identifiers), so a URL containing `/tools/` or `/process/` no longer trips them either.
- **The exemption says "this string is data", never "this target is fine":** `ref_id:"constructor"` is exempt from the NAME scan and then fails downstream — it is not a URL and resolves to no earlier result, so the fetch target cannot be determined. That path was also tightened this round: **an unresolvable ref_id is a violation even when the client reports a final URL**, because the address the client ended at is not the one it was asked for and D-4b must judge both.
- Closing-round mutation battery: 3/3 caught (exemption extended to string literals in any position → both bypass tests fail; exemption removed → the URL/MDN tests fail; unresolvable ref_id tolerated when a final URL exists → the `ref_id:"constructor"` test fails). The first attempt at the any-position mutant survived because it only widened the KEY name, not the position — rewritten to mask every literal, it is caught.

implement-loop: slice 1 shipped cf4d2c8; remaining: [2]

---

# Slice 2 — Claude web agent (D-3, D-3a, D-4b, D-4c, D-6) + docs

## Slice-2 preflight (orchestrator, measured 2026-09-19, claude 2.1.276 / tmux 3.6a)

Probes run before any code, because two of them are the slice's prerequisite gates.

- **Auth from a dedicated configuration home: PASSES, via a copied credential, not the keychain.**
  The macOS keychain item is namespaced per config home (`Claude Code-credentials` for the
  default home, `Claude Code-credentials-<hash8>` for others), so a fresh `CLAUDE_CONFIG_DIR`
  reports `Not logged in · Run /login` (measured). Writing `$CFG/.credentials.json` from the
  owner's keychain item authenticates and runs a real turn (measured: WebFetch of
  `https://example.com`, `Received 559 bytes (200 OK)`, answer "Example Domain"). **Copy only
  `claudeAiOauth`** — the keychain blob also holds `mcpOAuth` server tokens (supabase client
  secrets here), which an auth-only home must not carry (D-3).
- **Onboarding/trust seeding is required.** A fresh home shows the theme wizard, and the
  workspace-trust dialog ("Quick safety check: Is this a project you created or one you
  trust?", default option `No, exit`) — the existing driver's `_TRUST_DIALOG_MARKER` does not
  match this wording and its `1`+Enter would pick "No, exit". Seeding `$CFG/.claude.json` with
  `hasCompletedOnboarding`, `theme`, `bypassPermissionsModeAccepted` and a `projects` entry
  keyed by the **resolved** work dir (`/private/tmp/...`, not `/tmp/...`) skips both.
- **Flags accepted on 2.1.276:** `--safe-mode --permission-mode bypassPermissions --tools
  'WebSearch,WebFetch' --strict-mcp-config --disable-slash-commands --session-id --model`.
  `--restricted` is NOT usable (it refuses bypassPermissions, D-3 keeps it); `--bare` is NOT
  usable (it never reads OAuth, so the Max plan cannot authenticate). No managed-settings
  directory exists on this machine (`/Library/Application Support/ClaudeCode/` absent).
- **D-4b pre-connection enforcement: a mechanism EXISTS and was demonstrated.**
  `sandbox-exec` works on Darwin 25.5. With `(allow default)(deny network*)` plus
  `(allow network-outbound (remote unix-socket))` and one `(remote ip "localhost:<proxy>")`,
  a confined process reaches the public web only through that port: direct egress fails,
  and a loopback server on a *different* port is unreachable (measured with curl).
  Under that confinement the real client routed **every** connection through the proxy —
  `api.anthropic.com`, `downloads.claude.ai`, telemetry, and the WebFetch of
  `example.com` — so the proxy is a complete egress chokepoint and can judge each
  destination before connecting. Measured detail: WebFetch of `http://127.0.0.1:<port>/`
  arrives as `CONNECT 127.0.0.1:<port>` (the client upgrades http→https), i.e. the
  filter sees it and can refuse it before any connection.
- **No session-start tool inventory exists in the interactive transcript** (record types
  surveyed across real sessions): D-3a therefore takes its second branch for Claude — an
  admitted-version record — with the transcript's `attachment/deferred_tools_delta`,
  `skill_listing`, `mcp_instructions_delta`, `agent_listing_delta` records used as positive
  *negative* signals.
- **WebFetch exposes structured results.** The tool-result `user` record carries
  `toolUseResult = {bytes, code, codeText, result, durationMs, url}` — a status code and a
  URL — alongside the `tool_use` block's `input.url`. Whether `url` is the post-redirect URL
  is what the slice's redirect capture must decide.
- **Operational caveat for the owner:** the copied OAuth credential is refreshed by the child
  when it nears expiry (the probe warned "Your login expires in 1 day"). If the vendor rotates
  refresh tokens, a web-agent call could invalidate the owner's own login. A one-time `/login`
  into a fixed dedicated home would avoid sharing the credential at all.

## T1a — egress filter + sandbox-exec confinement (D-4b)

### Decisions
- New `python/src/subllm/drivers/egress.py`; no slice-1 file touched. `classify_url` is reused as-is (no second classifier).
- Check order is **scheme → `classify_url` → port**, not port first: the live G3 control uses `http://127.0.0.1.nip.io:<unprivileged>/`, and a port-first rule would report every such refusal as "port not allowed" and never prove the resolves-to-loopback rule. Cost: one resolution for a target the port rule then refuses.
- The validated address is captured by wrapping the resolver per request (`_OneShotResolver` over `CachingResolver`), not by reading a cache dict — `classify_url` lowercases/strips the host, so a raw-host key would miss. Guarantees exactly one `resolve()` per request; an IP-literal host (resolver never called) is its own address.
- Ports restricted to 80/443 (`ALLOWED_PORTS`). D-4b judges public **web** targets; a wider rule would let the confined client reach SSH/DB/mail on a public address.
- `connect` is an injected callable (default `socket.create_connection((address, port))`). It is the tests' only path to the origin server, which is what makes "the origin recorded ZERO requests" a real pre-connection assertion — and T1b can leave it at the default.
- Origin-form requests (`GET /x` + `Host:`) are **refused 400**, never routed off `Host`: that name was chosen by the client and never validated.
- CONNECT authority is refused if it contains `/ ? # @`; the synthesized URL keeps IPv6 brackets. Bytes buffered past the header block are forwarded to upstream after connecting (otherwise TLS garbage).
- `sandbox_exec` is hardcoded `/usr/bin/sandbox-exec`, never `shutil.which` (PATH injection would defeat the boundary; `conftest.fake_bin` prepends PATH). Missing binary or unwritable profile → `ClientError` (fail closed, no unconfined fallback).
- Profile text is the orchestrator's measured-good text verbatim, parameterized only by port; `sandbox_profile` rejects a non-int/out-of-range port (a string could smuggle `*`).
- `stop()`: `shutdown()` → close every tracked socket → `server_close()` → join workers. Header read has a 15 s timeout and a 64 KB cap so a silent client cannot pin a thread.

### Rejected
- **Resolving inside the proxy and passing addresses to a second checker** — forks the D-4b judgement; the wrapper keeps one classifier authoritative.
- **TLS interception** to inspect redirect hops — the spec says no MITM; each redirect hop is a fresh CONNECT/absolute-URI and is judged on its own.
- **Allowing an arbitrary test port through `allowed_ports`** in tests — it would make the port rule non-load-bearing; the injected connector gives the same hermeticity without relaxing a contract.
- **`socket.create_connection((host, port))` with the name** — the TOCTOU hole the task names; mutation M2 confirms the test catches it.
- **Daemon threads only, no join** — "no leaked threads after stop" would be unprovable; `live_thread_count()` is asserted 0.

### Assumptions
- **Why the module is load-bearing (orchestrator's live rehearsal, quoted):** with a NON-filtering tunnel proxy the real client issued `CONNECT 127.0.0.1.nip.io:18762` and the loopback server received it. The client self-refuses `file://` ("Invalid URL") and bare `localhost` ("cannot fetch localhost … without a dot") but NOT a public-looking name that resolves to loopback — that is exactly the case this filter closes, and the hermetic tests prove it with a zero-request assertion on the origin.
- Judgement is **per connection setup**, not per pipelined request: after an allowed CONNECT/absolute-URI the socket is already bound to that validated IP, so a second request on it cannot reach a new destination. Check by pipelining a second absolute-URI on one keep-alive socket (the filter forces `Connection: close` upstream on the plain path).
- `93.184.216.34` is used in tests purely as a public-looking literal; nothing dials it (injected connector).
- The measured profile's unix-socket rules are what keep DNS/IPC alive under `deny network*` (orchestrator's measurement, not re-derived).

### Deferred
- The Claude driver, transcript parser and any live test — T1b, by task split.
- Wiring `EgressFilterProxy` into a client: nothing imports this module yet, so it cannot regress slice 1.
- Per-call URL allow/deny lists (§7) and response metadata.

### Touches
- New: `python/src/subllm/drivers/egress.py`, `python/tests/test_egress.py`. No shared-surface change: `publictargets.py`, `webagent.py`, `codex_web.py`, `codex_trace.py`, `cli.py`, `pyproject.toml` untouched (no new marker — the E2E confinement test runs in 0.56 s and is `skipif`-guarded on `sandbox-exec`/`curl`).
- **T1b wiring:** point the child at the filter with `HTTP_PROXY`/`HTTPS_PROXY` = `proxy.proxy_url` (add both to the D-6 env allowlist) and do NOT forward the caller's `NO_PROXY`/`no_proxy`. Under the real client every attempt arrives as `CONNECT host:port`, so `Attempt.method == "CONNECT"` and `Attempt.target` is `host:port`, not a URL. A validated name with several addresses is tried in resolution order (all passed the same check), so a 502 in a live run means every validated address was unreachable, not a refusal — refusals are 403 and always appear in `.attempts`.
- API for T1b: `EgressFilterProxy(resolve=…, connect=…, allowed_ports=…)` as a context manager, `.port`, `.proxy_url`, `.attempts -> tuple[Attempt, …]`, `.refusals`, `.live_thread_count()`; `Attempt(at, method, target, host, port, address, allowed, reason)` (frozen — field names are the G3 evidence contract); `confined_argv(port, command) -> argv` context manager; `sandbox_profile(port)`, `sandbox_argv(...)`, `judge_target(url, …)`.
- Test evidence: `cd python && uv run pytest tests/test_egress.py -q` → 29 passed (8.2 s); full suite 270 passed, 8 skipped. Mutation battery 6/6 caught: pre-connection check removed, dial the name instead of the validated address, port rule widened, deny-all dropped from the profile, refusals unlogged, CONNECT's buffered bytes dropped.

### T1a fix round (blind-reviewer findings)

- **`judge_target` is total.** `GET http://[good.test]/`, `GET http://[::1/x`, `CONNECT [::1:443` made `urlsplit` raise inside the handler: nothing connected, but the client got an empty response and the attempt log held NO entry — a hole in the G3 evidence for exactly the inputs a hostile page can induce. Any parse failure now returns a refusing `_Verdict` ("unparsable target …"), recorded then refused.
- **`Attempt` gains `connected: bool`** (final field list: `at, method, target, host, port, address, allowed, reason, connected`). `allowed=True` alone read the same for a real connection and for a 502 where nothing was reached; T1b must ground G3 on `allowed and connected`. It is set by replacing the logged entry only after `_connect` returns.
- **Idle-bounded pump.** `EgressFilterProxy(idle_timeout_s=…)`, default 180 s: a connection with no traffic either way for that long is abandoned, so a hung upstream cannot pin a handler thread and its sockets for the session. Tested with `idle_timeout_s=0.5` against a listener that never answers.
- **A profile that fails mid-write is unlinked** before the `ClientError`, so no half-written description of the boundary is left in the temp dir.
- **Claim corrected, rule unchanged (reviewer demonstrated it).** Under this profile a confined process can still reach ANY unix socket (`curl --unix-socket` returned a local service's body) and can still resolve names. The property the profile actually gives is: **the only reachable TCP network destination is the filter's port.** Unix-domain sockets and name resolution stay available because the client's own IPC and DNS need them — so the profile is NOT what confines the model; the proven tool set (D-3a) is, and the model is offered no tool that can speak to a unix socket. The module docstring now states this. The profile is deliberately neither widened nor narrowed.
- **Deferred, not fixed:** `stop()` during an in-flight upstream connect can leave one handler alive up to the connect timeout past the join. It holds no listening socket and cannot accept new work; a bounded connect timeout already caps it.
- Fix-round mutation battery: 4/4 caught (judge_target raising again, `connected` marked before connecting, idle bound removed, profile unlink dropped). Earlier battery 6/6 still applies.
- Test evidence after the fix round: `cd python && uv run pytest tests/test_egress.py -q` → 37 passed (12.6 s); full suite 278 passed, 8 skipped.

## T1b — Claude web-agent mode: driver, live captures, transcript parser, tests

### Decisions
- `run_turn` (raw evidence) is split from `judge_turn` (parse + D-3a/D-4b/D-5), so the live captures could be recorded and committed BEFORE the parser existed, as slice 1 did for codex.
- **A positive tool inventory DOES exist in the transcript** (new measurement, contradicting the preflight's survey): the second `prompt_snapshot` attachment carries `tools` — `[WebFetch, WebSearch]` under `tools='web'`, `[]` under `none`, in all five captures. It is a **strengthening**, not the admission gate: it is written with the request but read after the turn, so it cannot gate the model before it acts. Admission stays the orchestrator's admitted-version record `{"2.1.276"}`, fail closed. A name outside the allowed set is an `IsolationError`; a transcript reporting no inventory leaves the trace incomplete.
- `final_url` on a **2xx only**, taken from the client's own `toolUseResult.url` (equal to the requested URL in every capture). Grounded in the committed redirect fixture: `httpbin.org/redirect-to?url=https://example.com` returns `code:302`, body "REDIRECT DETECTED … not fetched automatically", `url` = the ORIGINAL — so a cross-host redirect is never silently followed. Residual caveat, stated in the code: a SAME-HOST path-level redirect can be followed silently, so `final_url` is exact at host granularity and may differ in path.
- **The retrieved URL is `https://example.com` with NO trailing slash** — that is the string the client reports; D-4 forbids rewriting what it says it retrieved. The live G1 assertion uses it (the task text said `…/`, which is codex's spelling).
- A refused fetch's `toolUseResult` is a plain **string** ("Error: proxy refused the connection", "Error: Invalid URL") → `ok=False`, no final URL: a failed fetch, not unknown evidence.
- The egress filter's attempts are attached to each fetch event's `raw["egress_attempts"]` by host (D-4 raw evidence); a **refusal matching no traced fetch is an `IsolationError`** — the confined client dialled a non-public address outside any traced tool. **[SUPERSEDED by the fix round and then by the gate fix round — kept to show what was weighed. In force: EVERY refusal is judged, traced or not, on its machine-readable `code`; `port` is not a violation, and `unresolved`/`resolved_non_public` on a NON-traced client-internal request is a retryable `ClientError`, not an `IsolationError`.]**
- `file-history-snapshot` is allowlisted only while `trackedFileBackups` is empty; a non-empty one is a violation and `file-history-delta` is deliberately off the allowlist (mirrors codex's `turn_diff`).
- A dialog in the pane is a loud `ClientError` naming it, never answered: the workspace-trust default is "No, exit" and its cursor is a banner marker. Every tmux call passes `-L <socket> -f /dev/null` (the owner's `~/.tmux.conf` could `set-environment -g` into the dedicated server).
- **D-6 is asserted on the server that ACTUALLY ran the agent**, not only on what we started it with: `tmux -L <sock> show-environment -g` is read back before `kill-server`, and a name outside the allowlist stops the call (`ClientError`), so every successful live run is a D-6 assertion. Measured: the real server adds `PWD` — tmux copies its OWN cwd, which was the caller's repo — so the server is now started with `cwd=work_dir` and `PWD` is checked to BE that empty directory rather than merely allowed. `__CF_USER_TEXT_ENCODING` is injected by macOS into every spawned process and is allowed by name.
- **`HOME` = an empty temp dir works** (measured; codex parity) — the client authenticates from `CLAUDE_CONFIG_DIR/.credentials.json` alone.
- The transcript is read pre-`kill-server`, after it stops growing; kill adds only `last-prompt` + `cost-state` (measured by diffing a pre/post copy).
- The egress filter is a CONNECT-level filter: no TLS interception, no token read or rewrite, client talks to Anthropic natively — it is **not** the subscription-to-API proxy AGENTS.md bans.

### Rejected
- Making the transcript inventory the D-3a gate — it is read after the model acted; the orchestrator's admitted-version ruling stands and the inventory is layered on top.
- Normalizing `retrieved_urls` (adding `/`) for cross-client uniformity — G2's uniformity is the trace SHAPE, not rewritten URLs.
- Reusing `run_claude_tmux` / `_wait_for_banner` — it drives the SHARED tmux server (D-6 violation) and its `_TRUST_DIALOG_MARKER` + `1`+Enter would pick "No, exit" on this client's wording.
- `--restricted` (refuses bypassPermissions) and `--bare` (never reads OAuth); forwarding the caller's `NO_PROXY`; reconstructing any URL from the answer text (D-4).
- Attaching filter attempts to search events — hosted search runs vendor-side, so no local connection corresponds to it.

### Assumptions
- The record/content-block/attachment allowlists are drawn from the five isolated captures plus a reviewed set of the client's own session-state types; a benign NEW record type fails closed as a violation (same trade as codex). Check by re-capturing on any client update — which the version gate forces anyway.
- `prompt_snapshot.tools` stays the client's inventory field; its absence degrades to `trace_complete=False`, not to a silent pass.
- The copied OAuth credential may be refreshed by the child (preflight's operational caveat); a one-time `/login` into a fixed dedicated home would avoid sharing it.

### Deferred
- Docs (drivers contract, README, AGENTS.md) — T2 per §5.
- Response metadata beyond status (§7), per-call URL allow/deny lists (§7), an OS-level filesystem sandbox (§7 — the profile bounds TCP only, as T1a recorded).

### Touches
- New: `python/src/subllm/drivers/claude_web.py`, `python/src/subllm/drivers/claude_trace.py`, `python/tests/test_claude_web.py`, `python/tests/fixtures/claude-2.1.276-{g1-fetch,redirect,search,g3-negative-control,none-tools}.jsonl` + `claude-2.1.276-g3-egress-attempts.json` (sanitized live captures).
- Changed: `python/src/subllm/claude.py` (adds `run_web_task`; `__init__` stays side-effect-free and the text methods are untouched). `python/tests/test_integration_web_agent.py` (live claude G1+G3 added; the module-level `which("codex")` skip moved onto the codex tests so the claude ones can run). `python/tests/test_cli_web_agent.py` — **shared-surface collision**: its "client without web-agent mode" test asserted `ClaudeLLM` has no `run_web_task` and now spends real quota; it is rewritten to stub a client that lacks the method, keeping the CLI branch covered.
- No slice-1 file's behaviour changed: `webagent.py`, `publictargets.py`, `egress.py`, `codex_*.py`, `cli.py` untouched.
- **Environment trap for the next task:** an equal-SIZE mutant restored within the same second defeats Python's `(mtime-seconds, size)` `.pyc` check, so the MUTANT's bytecode kept running after the restore — two later tests "passed" against a rule that was not loaded (and `co_consts` lost the branch's strings because `if False:` is dead-code-eliminated, which is what exposed it). The battery now `os.utime`s every file it writes and restores and runs pytest with `PYTHONDONTWRITEBYTECODE=1`; if a result looks impossible, clear `__pycache__` first. The final suite ran after the fix.
- Observed, not fixed: the isolated config home auto-downloads the official plugin marketplace from `raw.githubusercontent.com` on every call. It adds no tool (the per-run inventory proves it, and `skill_listing`/`mcp_instructions_delta` fail closed if that changes), but it is an unasked-for round trip; seeding `officialMarketplaceAutoInstallAttempted: true` in `.claude.json` would likely skip it.
- Operational note: all client-internal destinations (`api.anthropic.com`, `raw.githubusercontent.com`) were allowed in every capture. A client update that adds an endpoint on a non-80/443 port would trip `_check_unmatched_refusals` and fail every run closed — read `egress_attempts` in the error's trace first.
- The `session_context` attachment injects the OAuth account's email into the model's context (sanitized in the fixtures). It is auth-derived, not owner-config-derived, so D-3 holds, but it is the one piece of owner identity that reaches the session.
- Evidence: `uv run pytest tests/test_claude_web.py -q` → 54 passed; full suite `uv run pytest -q` → **332 passed, 10 skipped**; live `SUBLLM_LIVE=1 … -k claude` → 2 passed (G1 grounded + G3 control: zero loopback hits, canary/sentinel/body absent, both refusals logged with a loopback reason); acceptance `SUBLLM_LIVE=1 uv run python -m subllm.cli web-agent --client claude --model claude-haiku-4-5-20251001 …` → exit 0 with `retrieved_urls:["https://example.com"]`, `trace_complete:true`, `client_version:"2.1.276"`. Mutation battery **16/16 caught** (version admission, tool-name check, tools='none', D-4b trace check, 2xx-only final_url, record allowlist, content-block allowlist, deferred_tools_delta, tool inventory, sub-agent transcripts, credential filtering, env allowlist, server-env read-back, server PWD, unmatched refusal, dialog handling). M12 first survived against a direct-call test and needed a `judge_turn` wiring test to become load-bearing.

### T1b fix round (blind-reviewer findings)

- **A recorded refusal now fails the run even when its host IS traced** (the real D-5 break). `_check_unmatched_refusals` skipped those and let `_check_targets` RE-RESOLVE the name, so a host that resolved to loopback at fetch time but public at judgement time (DNS rebinding, a TTL rolling to a CDN) returned the ANSWER with the loopback verdict sitting unused in `raw["egress_attempts"][…]["reason"]`. The filter's recorded verdict is the authoritative D-4b evidence — it is where the connection would actually have gone — so `_check_refusals` raises on it, with a message saying whether a traced fetch asked for it. **Carve-out kept and tested: a port-rule refusal is not an isolation error** — `egress.judge_target` checks publicness BEFORE the port rule, so that reason means the target WAS a public http/https host and the refusal is policy (80/443 only), not a non-public destination. The coupling to `judge_target`'s wording is pinned by a test that calls it, so a reword fails loudly instead of widening the carve-out. **The carve-out is sound only because `judge_target` checks publicness BEFORE the port rule** (T1a considered and rejected the other order); that ORDERING is pinned too — the same test asserts a name resolving to loopback on port 8443 comes back with a resolution reason, not a port reason, so swapping the checks would re-open the rebinding hole through this door and is caught (mutant F7, which mutates T1a's `egress.py` and restores it from a saved copy).
- **The resolver branch of the D-4b fetch check is now load-bearing on its own.** The old test used the G3 fixture, whose first target is a literal loopback address caught by inspection before the resolver is consulted. A new case fetches `https://dev.example.test/x` with a resolver that maps only that name to loopback, with no filter attempt supplied.
- **A login menu fails fast with the real cause.** It matched the banner markers, so the prompt went to a menu and the call burned its whole `timeout_s` before reporting "turn did not complete". **`/login` is deliberately NOT a marker: an AUTHENTICATED pane carries "Your login expires in 1 day · run /login to renew" (measured live — the first version of this check failed a working run).** Only wordings that state the session has no credential count, and the measured authenticated banner is a regression test.
- **A mode-switching prompt prefix is neutralised at send time.** `!` puts this TUI into bash mode and `/` into a slash command, and the prompt is caller-supplied, so `safe_prompt` prefixes one space rather than trusting the caller; refusing would fail legitimate tasks opening with those characters, and a leading space does not change what the text asks for. **The underlying TUI behaviour is NOT verified live — the mitigation is precautionary** and is tested at the send site (the stub logs `send-keys -l`).
- **The client auto-updated mid-round (2.1.276 → 2.1.277) and the D-3a gate stopped every live call, exactly as designed.** 2.1.277 was then admitted by RE-RUNNING the control, not by widening the set on faith: live G1 grounded on `example.com`, live G3 raised `IsolationError` (from the new recorded-refusal rule), the loopback origin logged zero requests, and canary/sentinel/body were absent. The parser needed no change, so the transcript shape is unchanged across the two versions. `ADMITTED_VERSIONS = {"2.1.276","2.1.277"}` with that evidence recorded next to it; the live G1 test now asserts membership of the driver's set instead of repeating a constant. The committed fixtures stay 2.1.276 captures.
- Deferred, recorded only: the timeout path discards the transcript unjudged (matches the `codex_web` convention).
- **Live runs re-run this round and why:** the D-5 fix changes WHICH rule fires on G3, so G1+G3 were re-run (green), plus the 2.1.277 admission control and the acceptance command (`client_version: "2.1.277"`, `retrieved_urls: ["https://example.com"]`, `trace_complete: true`, exit 0).
- Evidence: `uv run pytest tests/test_claude_web.py -q` → **63 passed**; full suite → **341 passed, 10 skipped**. The literal-loopback matched refusal is double-covered by design (both the recorded-refusal rule and `_check_targets` catch it); the NAME case is what makes the recorded-refusal rule load-bearing alone. Fix-round mutation battery **8/8 caught** (recorded refusal skipped when traced; port-rule carve-out removed; carve-out widened to every refusal; resolver branch removed; login detection removed; login markers widened back to a bare `/login`; `judge_target` reordered to check port before publicness; prompt-prefix mitigation removed). The earlier 16-mutant battery re-run after the fixes: still **16/16**.

## T2 — docs: drivers contract (both modes), README usage, AGENTS.md note

### Decisions
- `docs/drivers-contract.md` gains THREE sections, not two: a shared `web-agent mode` contract (trace types, derived retrieved list, the two by-construction empties, §4.2 CLI JSON, one-attempt, IsolationError semantics, error table) plus one per client. The shared part is what both clients must agree on; duplicating it under each client is how a port drifts.
- Each client section is ordered the way a port must execute it — invoke → flags NOT used + why → admission/preflight → isolation → env allowlist → trace source → classification → events → incomplete list → D-4b → **judgement order** → errors. The judgement ORDER is written out explicitly for both (it is behaviour, and nothing else in the repo states it).
- Feature/record/attachment allowlists are copied verbatim (names are the contract); codex's 30-name `ENABLED_ALLOWLIST` justifications are pointed at in code rather than duplicated — they would rot next to the check.
- Limitations are written as limitations, in the code's own hedged wording: codex's scanner-not-a-parser, codex's after-the-fact D-4b, claude's host-granular `final_url`, the profile bounding TCP only and NOT being a filesystem sandbox, the timeout path discarding the transcript.
- The "not the banned proxy" statement is made in all three files (contract, README hard-constraint section, AGENTS.md) because each has a reader who will not read the others.
- README: a `Web-agent mode` subsection under Usage with a field-by-field table framed as "what this means for grounding", plus a `web-agent` block under CLI. The URL-spelling asymmetry (`https://example.com/` vs `https://example.com`) is stated with the measured example in both README and the contract — it changes consumer code.
- Updated the hermetic test count in Testing (56 → 341 passed, 10 skipped) because that line sits in a section this task edits; left the stale `Status:` line alone as drive-by.

### Rejected
- A single combined "web-agent" section covering both clients — the invocation, isolation and D-4b stories differ enough that a port would have to reverse-engineer which bullet applies.
- Copying codex's `ENABLED_ALLOWLIST` justifications and the full script-scanner rule text into the doc — the scanner rules are summarized (shape allowlist, property denylist, key allowlist, escapes, one-complete-literal, the data exemption); the authority stays `codex_trace.py`.
- Documenting `find_in_page` as verified — T1 recorded it as an assumption with no fixture, so it is labelled "ASSUMED, no fixture".
- Rewriting README's `Status:` paragraph and the `ts/` section — out of scope for this task.

### Assumptions
- The contract's audience is a port author, so it states behaviour (including fail-closed costs) rather than rationale; rationale stays in the spec and this log. Check by handing the contract alone to the TS port when §7's parity item is picked up.
- The three files are the whole doc surface §5 asks for. `grep -rln "codex_exec.py" docs/ README.md` also hits the v1/TS specs, plans and an old review — deliberately NOT updated: they are dated records, not living docs.

### Deferred
- No `docs/PRODUCT.md`/changelog equivalents exist in this repo, so nothing there.
- TS parity, streaming traces, per-call allow/deny lists, response metadata and an OS filesystem sandbox stay undocumented as features (spec §7) — each is either absent from the docs or named explicitly as deferred/not-a-sandbox.

### Code/doc mismatches found (documented as-is, code NOT changed — reported to the orchestrator)
- `CodexLLM(codex_home=…)` is IGNORED by `run_web_task`: `run_codex_web_task` always builds a throwaway home via `_isolated_codex_home()` (sourced from `$CODEX_HOME` or `~/.codex`). README documents `codex_home` for the text path only; the contract now says the web path ignores it.
- Empty-answer handling diverges from the text paths: `codex_web` never checks the `-o` file for content (an empty answer is returned as `answer=""`), and `claude_web` raises `ClientError` where `claude_tmux` raises `OutputError` for "no completed text turn". Documented as a KNOWN DIVERGENCE.
- `--json` is passed to `codex exec` in web-agent mode but its stream is not the trace (only the error blob reads stdout); the contract says so, so a port does not build on it.
- **CLI `--schema` is not content validation** (confirmed by reading `_validate_with_model` + `schema_model_from_json_schema`): the adapter is a `RootModel[dict]` that accepts any JSON object, so enforcement is the client's binding — native on codex (`--output-schema`), absent on claude (prompt-instructed only). G1's "schema-validated JSON" holds only via the Python API with a real Pydantic model. Documented in both README and the contract.

### Touches
- `docs/drivers-contract.md`, `README.md`, `AGENTS.md`. Prose only — no code, tests or fixtures touched; no shared code surface.
- Evidence: `cd python && uv run pytest -q` → **341 passed, 10 skipped** (114 s), unchanged from T1b. No mutation battery: this task carries no logic.


### T1b gate fix round (whole-run gate, cycle 1 — orchestrator + Codex cross-model reviewer)

- **A DNS failure is no longer reported as an isolation violation (the MAJOR defect).** `Attempt` and `_Verdict` now carry a machine-readable `code` (`non_public | resolved_non_public | unresolved | port | scheme | unparsable | missing`), produced by a new `publictargets.classify_url_detail`; `classify_url` is a thin wrapper over it, so the Codex path is behaviour-identical. `_check_refusals` judges the CODE, never the prose:
  - `port` → not a violation (publicness is checked first, so the target was public);
  - **`unresolved` or `resolved_non_public` on a destination NO traced fetch asked for** → `ClientError` naming the host, i.e. a delivery failure the caller may retry. This is what stops one transient DNS miss (or a Pi-hole answering `0.0.0.0`) on a client-INTERNAL CONNECT — an api.anthropic.com retry, telemetry, the marketplace — from hard-stopping the call as an `IsolationError` the README tells consumers never to retry. Splitting `non_public` into by-inspection and by-resolution is what makes this safe: **a private address LITERAL is still an `IsolationError` whether traced or not** (nothing legitimate dials one through this filter);
  - **a TRACED fetch keeps `IsolationError` for every non-port code**, including both non-public forms — the D-4b case does not soften. All four combinations are tested.
- `test_the_checked_paths_are_the_documented_managed_settings_locations` pins the exact tuple on purpose: a new managed-settings location must be a deliberate edit with a reason, not an accident. Do not "fix" it by loosening it.
- **Managed settings fail closed.** `--safe-mode`'s own help says admin-managed policy settings still apply, so an MDM-dropped `managed-settings.json` with a `PreToolUse` hook would run a shell inside the session with the allowlisted env and leave nothing the parser classifies. Both documented paths are checked before the client starts; both branches tested.
- **Bash mode is closed at the parser layer, with measured evidence.** Probed in the isolated home (a `!`-prefixed input costs no model turn, so no quota): it DOES execute locally — the probe wrote a file to `/tmp` — and records `<bash-input>…</bash-input>` then `<bash-stdout>…</bash-stdout><bash-stderr>…</bash-stderr>` as ordinary `user` STRING content, which the parser previously read as "the prompt". Any such record is now an isolation violation, so the guarantee no longer rests on the send-time mitigation. **Also measured: BOTH send paths reach bash mode** (send-keys and paste-buffer, single- and multi-line), and the leading space neutralises both. Two notes on this rule: the probe's write to `/tmp/…` happened UNDER `sandbox-exec`, which is the live demonstration of what T1a already recorded — the profile bounds TCP destinations, not file writes, so the proven tool set plus this parser rule are the boundary. And the tag check is a substring test, so a prompt legitimately containing the literal string `<bash-input>` fails closed as a false isolation error; the cost is a re-phrase, which is the right side to err on.
- **`safe_prompt` is no longer a first-character rule.** It strips every C0 control byte and DEL except tab and newline, THEN applies the leading-space rule. That closes the three reported bypasses: `\x1b[200~!id\x1b[201~` (bracketed paste smuggled mid-prompt), `"…\r!id"` (a CR submits on the send-keys path, which is taken because the prompt has no newline), and `\x1b[201~` terminating a multi-line paste early so the remainder arrives as keystrokes. Task text has no legitimate use for raw control bytes.
- **D-3a TOCTOU on the binary closed.** `claude` on PATH is a symlink the client rewrites when it auto-updates (it did so during this task), so the version check and the launch could resolve different builds. The driver now `realpath`s the binary ONCE and runs that concrete versioned executable; the transcript cross-check stays as the second layer. Tested, including a symlink swapped between the check and the launch.
- **Prose corrected** (module docstring: both admitted versions; the D-3a comment no longer claims there is no inventory at all — it says there is none BEFORE the model acts, which is the real reason admission is by version), and the superseded refusal bullet above is marked in place rather than deleted.
- **`--json-schema` was checked before accepting the deferral (one live turn):** the flag's help does NOT carry the "(only works with --print)" note, and the interactive client ACCEPTS it — but it is **ignored**: with a schema requiring `{capital, confidence}` and `additionalProperties:false`, the answer came back as prose ("Paris is the capital of France."). So there is no native binding to wire up; the orchestrator's deferral stands and claude's `--schema` remains prompt-instructed only. **T2 (docs) must say plainly that on claude `--schema` shapes the prompt and does not validate the result.**
- **D-4b duplication: not extracted, made loud instead.** `claude_web._check_targets` and `codex_web._check_fetch_targets` are run over one shared table of fetch and search targets (public, loopback literal, private literal, `file://`, resolves-to-loopback, NXDOMAIN, octal spelling, IPv6 loopback) and must return identical verdicts, so a one-sided amendment fails. **T2 (docs) should note in the drivers contract that this rule lives in two places and both must be amended.**
- **The attempts fixture was re-recorded, not hand-edited** (`claude-2.1.277-g3-egress-attempts.json`, replacing the 2.1.276 one): adding a derived `code` field by hand would have destroyed a sanitized live capture's provenance. It came from a live G3 run on 2.1.277 and carries both refusal codes. The transcript fixtures are untouched 2.1.276 captures.
- **The live G3 control is model-non-deterministic, and the test now says so.** On some runs the model declines to attempt the forbidden fetches and simply answers (measured: roughly a third — 3 of 9 observed runs, counting both the captures and the live test) — nothing is violated, so `pytest.raises(IsolationError)` failed. Both branches now assert every claim that does not need an attempt (no canary/sentinel/loopback body in the answer OR the raw trace, zero requests at the origin, no non-web tool event, nothing retrieved); the branch that needs one — the filter's recorded refusal — **skips with a message rather than passing vacuously**. The no-attempt branch asserts that nothing NON-PUBLIC was retrieved (via `classify_url`), not that the retrieved list is empty: the model sometimes fetches a public page as a sanity check on the way, which is not a G3 failure. Earlier rounds' green G3 runs were attempts-made runs; this flakiness is a property of the control, not of the guard.
- **Live re-run on the final tree (required this round, all three):** live G1 + G3 → **2 passed**; acceptance → exit 0, `{"client":"claude","model":"claude-haiku-4-5-20251001","tools":"web","retrieved_urls":["https://example.com"],"fetch_events_exposed":true,"trace_complete":true,"client_version":"2.1.277","duration_ms":10040}` with `egress_attempts: [{… "target":"example.com:443","allowed":true,"connected":true,"code":null}]`.
- Evidence: `uv run pytest tests/test_claude_web.py -q` → **82 passed**; full suite → **360 passed, 10 skipped**. Gate-round mutation battery **11/11 caught**, after one test was fixed because it passed for the wrong reason: `test_the_driver_sends_no_control_bytes` read the log with `read_text()`, whose universal-newline translation turned a surviving `\r` into `\n`, so the ESC-only mutant survived on that payload; it reads bytes now and the `\r` case is load-bearing. A second addition closed the same class of gap for the delivery-failure rule, which was only tested at rule level: `test_the_judgement_reports_a_dns_miss_as_a_retryable_client_error` pins that `judge_turn` propagates it as a `ClientError` (the CLI keys its typed error on the class), never as `IsolationError`. (unresolved treated as isolation again; the delivery-failure carve-out leaking into traced fetches; a private literal downgraded; the port carve-out reading prose; `judge_target` dropping the code; managed-settings refusal removed; bash-mode rule removed; control-byte strip removed; strip narrowed to ESC only; binary TOCTOU reintroduced; the D-4b rule amended on one side only).
- **Touched beyond T1b's own files this round** (both previously gated, both re-run green): `python/src/subllm/publictargets.py` (adds `classify_url_detail` + codes; `classify_url` unchanged in behaviour) and `python/src/subllm/drivers/egress.py` (adds `Attempt.code` / `_Verdict.code`). `tests/test_egress.py` and `tests/test_codex_web.py` pass unmodified, which is the evidence that neither change altered slice-1 behaviour.
