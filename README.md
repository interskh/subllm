# subllm

**Run LLM requests through your flat-rate coding subscriptions (Codex/ChatGPT
plan, Claude Pro/Max) via the genuine official clients — no per-token API
billing, no request-rewriting proxy.**

`subllm` drives the real official CLIs as subprocesses (`codex exec`, and
interactive `claude` over tmux) behind a small `BaseLLM` seam, so any project
that already codes against `complete` / `complete_json` / `complete_json_schema`
can swap a per-token client (e.g. `GeminiLLM`) for a subscription-native one with
no call-site changes.

**Status:** v1 implemented (CodexLLM + ClaudeLLM + FallbackLLM + CLI). 56 hermetic
tests passing, plus a 6-test opt-in live suite — both the codex and claude paths
verified end-to-end against live subscriptions.

## What it does

- **Summarize / search-and-summarize on subscription quota** instead of paying
  per-token API rates for work your coding plans already cover.
- **`CodexLLM` (primary)** — wraps `codex exec` in an isolated `CODEX_HOME`,
  read-only and config-isolated; supports native structured output
  (`--output-schema`) and native web search.
- **`ClaudeLLM` (fallback)** — drives *interactive* `claude` in an ephemeral tmux
  pane (not `claude -p`, which bills a separate programmatic credit after
  2026-06-15), detecting turn completion by tailing the JSONL transcript.
- **`FallbackLLM`** — explicit, opt-in resilience: try the next client only on the
  exception types you name (default `QuotaError`); everything else fails loud.
- **`RegionGuard`** — optional preflight that blocks a call when your public IP is
  in the wrong region (e.g. VPN dropped). Whitelist (`allowed_regions`) or
  blacklist (`blocked_regions`) mode; off by default.
- **`DryRunLLM`** — a no-op client that records prompts, for your own tests.

## Layout

```
subllm/
  python/                # Python library (CodexLLM + ClaudeLLM + FallbackLLM + CLI)
    src/subllm/
      __init__.py          # public exports (the 11 names below)
      base.py              # BaseLLM ABC + transient _retry + DryRunLLM
      errors.py            # SubllmError / QuotaError / ClientError / OutputError / RegionError
      preflight.py         # RegionGuard (optional region/IP check)
      codex.py             # CodexLLM  (primary)
      claude.py            # ClaudeLLM (fallback)
      fallback.py          # FallbackLLM(primary, *fallbacks, on=(QuotaError,))
      cli.py               # minimal CLI: subllm complete / complete-json
      drivers/
        codex_exec.py      # contract for `codex exec`
        claude_tmux.py     # tmux driver for interactive `claude`
    tests/
    pyproject.toml
  ts/                    # TypeScript SDK (CodexLLM, phase 1)
    src/
      index.ts           # public exports
      base.ts            # BaseLLM + retry + ajv validation + DryRunLLM
      errors.ts          # SubllmError / QuotaError / ClientError / OutputError
      codex.ts           # CodexLLM
      drivers/codexExec.ts  # the isolated `codex exec` subprocess boundary
    test/
    package.json
  docs/                  # shared: spec, plans, research, drivers-contract.md
    superpowers/specs/   # design spec (PRD)
    superpowers/plans/   # implementation plan
    research/            # background research + sourcing
    drivers-contract.md  # subprocess contract (cross-language)
  README.md
```

## Requirements

- **Python ≥ 3.12**, `pydantic ≥ 2.6`.
- For `CodexLLM`: the **`codex` CLI** on `PATH`, logged in to a ChatGPT plan
  (verified on codex-cli 0.133.0 and 0.136.0).
- For `ClaudeLLM`: **`tmux`** and the **`claude`** CLI on `PATH`, logged in to a
  Claude Pro/Max subscription.

## Install

`subllm` is a local library other projects depend on by path. With `uv`:

```bash
# from the consuming project
uv add /path/to/subllm/python
```

or pin it as a path source in the consumer's `pyproject.toml`:

```toml
[project]
dependencies = ["subllm"]

[tool.uv.sources]
subllm = { path = "/path/to/subllm/python" }
```

Editable install also works: `uv pip install -e /path/to/subllm/python`.

## Quickstart

```python
from subllm import CodexLLM

llm = CodexLLM(model="gpt-5.4", codex_home="/tmp/codex-clean")  # see CODEX_HOME note
print(llm.complete("Summarize the French Revolution in two sentences."))
```

> **CODEX_HOME note:** point `codex_home` at a directory containing only a copy
> of your real `~/.codex/auth.json`. This keeps `codex exec` from silently loading
> your `AGENTS.md` / skills / config (context pollution). If you omit it, subllm
> uses the `CODEX_HOME` environment variable.

## Usage

### Drop-in `BaseLLM`

All clients implement the same three methods, so they are interchangeable:

```python
def complete(self, prompt: str) -> str: ...
def complete_json(self, prompt: str) -> dict: ...
def complete_json_schema(self, prompt: str, schema_model: type) -> dict: ...  # Pydantic
```

### CodexLLM (primary)

```python
from subllm import CodexLLM

llm = CodexLLM(
    model="gpt-5.4",          # optional; passed to `codex exec -m`
    reasoning_effort="medium",
    search=False,             # enable codex web search for this call
    codex_home="/tmp/codex-clean",
)
text = llm.complete("...")
```

### ClaudeLLM (fallback)

```python
from subllm import ClaudeLLM

llm = ClaudeLLM(model="claude-sonnet-4-6", timeout_s=300)
text = llm.complete("...")    # spawns an ephemeral tmux `claude` session
```

### FallbackLLM (explicit, fail-loud)

```python
from subllm import CodexLLM, ClaudeLLM, FallbackLLM, QuotaError

llm = FallbackLLM(CodexLLM(...), ClaudeLLM(...), on=(QuotaError,))
text = llm.complete("...")    # on QuotaError, tries Claude; any other error propagates
```

Only the exception types in `on` trigger a fallback. An `OutputError` (malformed
result) is a real bug, not a budget problem — it propagates immediately.

### Structured output (Pydantic)

```python
from pydantic import BaseModel
from subllm import CodexLLM

class Summary(BaseModel):
    topic: str
    bullets: list[str]

data = CodexLLM(...).complete_json_schema("Summarize this thread: ...", Summary)
# CodexLLM binds the schema natively (--output-schema) AND re-validates against
# the model; ClaudeLLM prompt-instructs the schema then validates. Either way the
# returned dict is guaranteed to satisfy `Summary`.
```

### Region preflight (optional)

```python
from subllm import CodexLLM, RegionGuard

# Whitelist: subscription valid only in these regions — block unless inside them.
guard = RegionGuard(allowed_regions={"US", "JP"})   # checks public IP before calling

# Blacklist: tunneling out of a banned home region — block only if you land there.
guard = RegionGuard(blocked_regions={"CN", "RU"})   # any other exit passes

llm = CodexLLM(..., region_guard=guard)             # raises RegionError if out of region
```

Pass **exactly one** of `allowed_regions` / `blocked_regions` (both or neither
raises `ValueError`). `RegionError` is a hard stop and is **not** a default
`FallbackLLM` trigger — if you're out of region, every subscription client is
equally blocked.

### DryRunLLM (for your tests)

```python
from subllm import DryRunLLM

llm = DryRunLLM()
llm.complete("hello")
assert llm.captured == ["hello"]    # records prompts, makes no real call
```

## CLI

A minimal CLI for smoke-testing and shell-out from non-Python projects:

```bash
subllm complete       --client codex|claude  [--model M] [--effort E] [PROMPT]
subllm complete-json  --client codex|claude  [--schema schema.json] [PROMPT]
```

`PROMPT` comes from the argument or stdin (`-`). Result goes to stdout; errors to
stderr with a non-zero exit code. Example:

```bash
CODEX_HOME=/tmp/codex-clean subllm complete --client codex "Say hello in five words."
```

## Testing

The default suite is **hermetic** — it stubs `codex`/`tmux` with fake executables
on `PATH`, so it never touches a real subscription or the network:

```bash
cd python
uv run pytest                 # 56 tests, no subscription, no network
```

A separate **opt-in live suite** drives the real subscriptions to confirm both
clients work end-to-end. It is skipped unless `SUBLLM_LIVE=1` is set (so CI and a
normal `pytest` run never bill you), and each client self-skips if its binaries
aren't on `PATH`; a `QuotaError` becomes a skip rather than a failure:

```bash
SUBLLM_LIVE=1 uv run pytest -m integration            # codex + claude
SUBLLM_LIVE=1 uv run pytest -m integration -k codex   # codex only
SUBLLM_LIVE=1 uv run pytest -m integration -k claude  # claude only
```

Requires `codex` logged in to a ChatGPT plan; the claude tests additionally need
`claude` (logged in to Pro/Max) and `tmux`.

## TypeScript SDK (`ts/`)

A Node ESM SDK that drives `codex exec` with the same capabilities as the Python
`CodexLLM`. Phase 1 ships `CodexLLM` + `DryRunLLM` only.

Consume it as a local path dependency (mirrors the Python `uv add /path` model):

```jsonc
// your-app/package.json
{
  "dependencies": {
    "subllm": "file:/path/to/subllm/ts"
  }
}
```

```ts
import { CodexLLM, QuotaError } from "subllm";

const llm = new CodexLLM({
  model: "gpt-5.4-mini",   // forwarded verbatim to `codex exec -m`
  search: true,            // -c web_search="live"
  codexHome: "/tmp/codex-clean",
});

// classify a topic, structured against a plain JSON Schema object:
const classified = await llm.completeJsonSchema(prompt, CLASSIFIER_SCHEMA);
```

- `completeJsonSchema(prompt, jsonSchema)` accepts a **plain JSON Schema object**,
  binds it to codex via `--output-schema`, and returns a parsed object that is
  **validated with ajv** (throws `OutputError` on mismatch). This is the reliable
  path for structured output.
- `completeJson(prompt)` is best-effort: it prompt-instructs a bare JSON object and
  parses codex's final message directly (no code-fence stripping), retrying on
  unparseable output. For schema-guaranteed results, prefer `completeJsonSchema`.
- Errors mirror the Python hierarchy: `QuotaError` (not retried), `ClientError`,
  `OutputError`, all under `SubllmError`.
- Concurrent calls are safe (each uses its own temp output/schema file).

### Build & consume

```sh
npm --prefix ts install      # installs deps AND builds dist/ (prepare runs tsc)
npm --prefix ts run build    # rebuild dist/ after changes
npm --prefix ts test         # run the vitest suite
```

> **Prerequisite for `file:` consumers:** the `file:` dependency relies on `subllm/ts`
> having its dev deps installed so the `prepare` script (`tsc`) can build `dist/` at
> install time. Run `npm --prefix ts install` in this repo once before a consumer
> (e.g. your-app) runs `npm install`. (npm runs `prepare` for a `file:`/directory
> dep but does not install that dep's devDependencies first, so `tsc` must already
> be present in `ts/node_modules`.)

## API reference

| Export | Signature |
|---|---|
| `CodexLLM` | `(model=None, reasoning_effort="medium", search=False, codex_home=None, region_guard=None, attempts=3, timeout_s=120)` |
| `ClaudeLLM` | `(model=None, permission_mode="bypassPermissions", region_guard=None, attempts=3, timeout_s=300)` |
| `FallbackLLM` | `(primary, *fallbacks, on=(QuotaError,))` |
| `RegionGuard` | `(allowed_regions=None, blocked_regions=None, lookup=default_lookup, ttl_s=300.0, on_lookup_failure="block")` — pass exactly one of allowed/blocked |
| `DryRunLLM` | `()` |
| `BaseLLM` | abstract base; subclass to add a client |

## Errors

All inherit `SubllmError`:

- `QuotaError` — subscription quota exhausted. The only default `FallbackLLM` trigger.
- `ClientError` — subprocess crash, missing binary, auth missing/invalid, tmux failure.
- `OutputError` — empty output, invalid JSON, or schema-validation mismatch.
- `RegionError` — preflight found the public IP out of region. Hard stop.

`CodexLLM`/`ClaudeLLM` retry only transient failures (`ClientError`/`OutputError`)
within a single client with 1s/2s/4s backoff; cross-client failover is
`FallbackLLM`'s job alone.

## Hard constraint

Client-native execution only. subllm **never** builds or uses a
subscription-to-API HTTP proxy (a token wrapped behind an OpenAI-compatible
endpoint) — that rewrites request shapes, isn't client-native, and carries
account-ban risk. It only ever drives the real official CLIs as subprocesses.

## Docs

- Design spec (PRD): [`docs/superpowers/specs/2026-06-02-subllm-v1-design.md`](docs/superpowers/specs/2026-06-02-subllm-v1-design.md)
- Implementation plan: [`docs/superpowers/plans/2026-06-02-subllm-v1.md`](docs/superpowers/plans/2026-06-02-subllm-v1.md)
- Subprocess contract (TS/Go ports): [`docs/drivers-contract.md`](docs/drivers-contract.md)
- Background research: [`docs/research/`](docs/research/)
