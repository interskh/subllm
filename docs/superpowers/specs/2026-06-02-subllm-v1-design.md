# subllm — Design Spec (2026-06-02)

## Purpose

A Python package your projects import to run LLM requests — summarization and
search-and-summarize (e.g. group-chat analysis) — through your **flat-rate
coding subscriptions** (Codex/ChatGPT plan, Claude Pro/Max) via the **genuine
official clients**. Goal: stop paying per-token for the Gemini API on work the
subscriptions already cover.

**Hard constraint (non-negotiable):** client-native execution only. NO
subscription-to-API proxies (HTTP shims that wrap a subscription auth token
behind an OpenAI-compatible endpoint). Those rewrite request shapes, aren't
client-native, and carry account-ban risk. subllm only ever drives the real
official CLIs as subprocesses. See `docs/research/research_subscription-summarization_2026-06-02.md`
for the full rationale and sourcing.

## Background facts that shape the design

(from the research doc — read it before implementing)

- **`codex exec`** is the natively non-interactive Codex subcommand. Under
  ChatGPT login it runs on the flat subscription (no API key, no
  programmatic/interactive billing split). Clean stdout (final message only),
  native `--output-schema`, native web search. **Gotcha:** it silently loads
  `AGENTS.md`/`config.toml`/skills → context pollution; must run isolated.
- **Claude:** after 2026-06-15 `claude -p` / Agent SDK draw from a *separate
  monthly programmatic credit*, but **interactive** Claude Code stays on the flat
  subscription. So the Claude path must drive the *interactive* client via tmux,
  not `claude -p`. A mature driver exists in `ralph-loop`'s `ralph_lib/drivers.py`;
  subllm adapts a **decoupled copy** (ralph-loop stays untouched).
- The target consumer (`your-app`) already has a `BaseLLM` ABC
  (`complete` / `complete_json` / `complete_json_schema`) with `GeminiLLM`.
  subllm mirrors this contract so it is drop-in.

## Decisions (locked during brainstorming)

| Decision | Choice |
|---|---|
| Consumption | **Python library first**; architected so a future TS/Go lib can port the driver layer. |
| Clients in v1 | **Both**: `CodexLLM` (primary) + `ClaudeLLM` (fallback). |
| Claude driver | **Decoupled adapted copy** of `ralph_lib`'s tmux driver, stripped of ralph's bash-watchdog event emission. |
| Fallback | **Caller chooses per-call.** No hidden auto-retry across clients. Explicit `FallbackLLM` composite for opt-in resilience. Base clients fail loud. |
| CLI | **Yes, minimal** — a dev/smoke-test tool that also serves as the interim shell-out path for non-Python consumers. |
| Region preflight | **Optional, config-gated** IP/region check before calling a client (guards against VPN-off → out-of-region requests). Off by default. |
| Name / home | `subllm` at `~/Projects/subllm`. |

## Interface (drop-in with the existing `BaseLLM` seam)

```python
class BaseLLM(ABC):
    def complete(self, prompt: str) -> str: ...
    def complete_json(self, prompt: str) -> dict: ...
    def complete_json_schema(self, prompt: str, schema_model: type) -> dict: ...  # Pydantic
```

A consumer swaps `GeminiLLM(...)` → `CodexLLM(...)` with no call-site changes.

Public exports: `BaseLLM`, `CodexLLM`, `ClaudeLLM`, `FallbackLLM`, `DryRunLLM`,
and the error types.

## Module layout

```
~/Projects/subllm/
  pyproject.toml                 # uv; src layout; console_scripts: subllm = subllm.cli:main
  src/subllm/
    __init__.py                  # public exports
    base.py                      # BaseLLM ABC + _retry (transient-only backoff)
    errors.py                    # SubllmError, QuotaError, ClientError, OutputError, RegionError
    preflight.py                 # optional region/IP guard (shared by both clients)
    codex.py                     # CodexLLM  (primary)
    claude.py                    # ClaudeLLM (fallback)
    fallback.py                  # FallbackLLM(primary, *fallbacks, on=(QuotaError,))
    cli.py                       # minimal CLI (complete / complete-json)
    drivers/
      __init__.py
      codex_exec.py              # subprocess contract for `codex exec`
      claude_tmux.py             # decoupled, adapted tmux driver (port from ralph_lib)
  tests/                         # stub `codex`/`tmux` binaries on PATH; no real calls
  docs/
    design.md                    # this file
    research/…                   # research context (already copied in)
    drivers-contract.md          # documents the subprocess contract for future TS/Go ports
```

The two files under `drivers/` are the **isolated subprocess contract** — the
"leave room for TS/Go" boundary. A future port reimplements only those, per
`docs/drivers-contract.md`.

## Clients

### CodexLLM (primary)
- Drives `codex exec` in an **isolated `CODEX_HOME`** (a dir containing only a
  copy/symlink of the real `auth.json`) plus `-s read-only --skip-git-repo-check
  --ignore-user-config` to avoid context pollution.
- `complete` → final-message stdout (use `-o <tmpfile>` for robustness).
- `complete_json` → instruct JSON in the prompt, parse, validate non-empty dict.
- `complete_json_schema` → write `schema_model.model_json_schema()` to a temp
  file, pass `--output-schema <file>`; parse + validate.
- Constructor: `model`, `reasoning_effort`, `search: bool`, `codex_home`,
  `attempts`, `timeout_s`.

### ClaudeLLM (fallback)
- Adapted tmux driver: spawn an ephemeral tmux session running **interactive**
  `claude`; send the prompt; **tail the JSONL transcript** (`CLAUDE_CONFIG_DIR`
  / `~/.claude/projects/...`) for an assistant record with terminal
  `stop_reason` (`end_turn`); return the final assistant text. Pane-capture used
  only for heartbeat/stuck-detection.
- Pre-grant tools (or run with tools disabled) so an unattended turn never
  deadlocks on an approval prompt.
- `complete_json` / `complete_json_schema` → prompt-instruct JSON + Pydantic
  validation (no native schema binding on this path).
- Constructor: `model`, `permission_mode`, `timeout_s`, plus tmux/session knobs.

## Region preflight (optional, config-gated)

Some users' subscriptions are only valid from supported regions; if their VPN is
disconnected their public IP may fall outside it. Firing a request in that state
can fail or risk the account, so subllm can check **before** the call.

- `preflight.py` exposes a `RegionGuard` with a `check()` method.
- Off by default. Enabled per-client via constructor: `region_check=True`
  (or pass a configured `RegionGuard`), with `allowed_regions: set[str]`
  (ISO country codes) and an optional `ttl_s` cache.
- On enable: before the first call (cached for `ttl_s`), resolve the current
  public IP's country via a pluggable lookup (default: a small HTTP geo-IP
  service; the lookup fn is injectable so it can be stubbed/replaced). If the
  country is not in `allowed_regions`, raise **`RegionError`** with a clear
  message ("public IP in <CC>; expected one of {…} — is your VPN connected?").
- `RegionError` is a hard stop, **not** a `FallbackLLM` trigger by default
  (if you're out-of-region, every subscription client is equally blocked —
  falling through would just risk a second account). Callers may opt in.
- Failure of the lookup itself (network down, service error) is configurable:
  `on_lookup_failure="block" | "allow"` (default `block` — fail loud rather than
  fire a possibly-out-of-region request).

## Fallback (explicit, fail-loud)

```python
llm = FallbackLLM(CodexLLM(...), ClaudeLLM(...), on=(QuotaError,))
```
`FallbackLLM` catches only the exception types named in `on` (default
`QuotaError`) and tries the next client; anything else propagates immediately.
Per-call client choice = just instantiate the one you want. No magic.

## Error handling

`errors.py`:
- `SubllmError` — base.
- `QuotaError` — codex 5h/weekly cap, claude programmatic-credit / subscription
  limit, auth-expired-due-to-quota. The only default fallback trigger.
- `ClientError` — subprocess crash, missing binary, auth missing/invalid,
  tmux/session failure.
- `OutputError` — empty output, invalid JSON, schema-validation mismatch.
- `RegionError` — preflight found the public IP outside `allowed_regions` (or the
  lookup failed under `on_lookup_failure="block"`). Hard stop; not a default
  fallback trigger.

`_retry` lives in `base.py` and wraps only **transient** failures within a single
client (mirrors the existing your-app backoff: 1s/2s/4s). It does NOT
cross clients — that is `FallbackLLM`'s job.

## CLI (minimal)

```
subllm complete       --client codex|claude  [--model M] [--effort E] [PROMPT]
subllm complete-json  --client codex|claude  [--schema schema.json] [PROMPT]
```
- PROMPT from arg or stdin (`-`).
- Prints result to stdout; errors to stderr with non-zero exit.
- Purpose: manual smoke-testing of both clients, and an interim way for
  non-Python projects to shell out before a native lib exists.

## Testing (intent, not just behavior)

- **No real subscription calls in tests.** Put stub `codex` / `tmux`
  executables on `PATH` (tiny scripts emitting canned output / canned JSONL).
- Cover:
  - CodexLLM: temp schema-file generation, `--output-schema` wiring, isolated
    `CODEX_HOME` is used, stdout parsing, error mapping (cap message → QuotaError,
    crash → ClientError, bad JSON → OutputError).
  - ClaudeLLM: JSONL completion-detection (only trust records with `stop_reason`
    set; ignore partial streaming lines), final-text extraction, approval-deadlock
    avoidance.
  - FallbackLLM: fires on `QuotaError`, does NOT fire on `OutputError` (encodes
    *why* — a malformed result is a real bug, not a budget problem).
  - RegionGuard: stub the IP-lookup fn → in-region passes; out-of-region raises
    `RegionError` and the client call is never attempted; lookup failure honors
    `on_lookup_failure`; result is cached within `ttl_s`.
- `DryRunLLM` provided for consumers' own tests.

## Implementation sequencing

1. `base.py`, `errors.py`, `preflight.py` (RegionGuard), `DryRunLLM`,
   pyproject + uv setup, tests scaffold.
2. **CodexLLM end-to-end** (independently shippable — delivers value first),
   wired to the optional RegionGuard preflight.
3. CLI (`complete` / `complete-json` for codex).
4. **ClaudeLLM** via adapted tmux driver (the heavy part).
5. `FallbackLLM` + cross-client docs.
6. `docs/drivers-contract.md` for future TS/Go ports.

## Out of scope (v1)

- Streaming responses.
- Async API.
- TS/Go libraries (only leaving room for them).
- Any HTTP server / proxy (forbidden by the hard constraint).
- Automatic cross-client fallback heuristics beyond explicit `FallbackLLM`.
