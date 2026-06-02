# subllm

**Run LLM requests through your flat-rate coding subscriptions (Codex/ChatGPT
plan, Claude Pro/Max) via the genuine official clients — no per-token API
billing, no request-rewriting proxy.**

`subllm` drives the real official CLIs as subprocesses (`codex exec`, and
interactive `claude` over tmux) behind a small `BaseLLM` seam, so any project
that already codes against `complete` / `complete_json` / `complete_json_schema`
can swap a per-token client (e.g. `GeminiLLM`) for a subscription-native one with
no call-site changes.

**Status:** v1 implemented (CodexLLM + ClaudeLLM + FallbackLLM + CLI), 46 tests
passing, codex path verified end-to-end against a live subscription.

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
- **`RegionGuard`** — optional preflight that blocks a call if your public IP is
  out of region (e.g. VPN dropped), off by default.
- **`DryRunLLM`** — a no-op client that records prompts, for your own tests.

## Layout

```
subllm/
  src/subllm/
    __init__.py          # public exports (the 11 names below)
    base.py              # BaseLLM ABC + transient _retry + DryRunLLM
    errors.py            # SubllmError / QuotaError / ClientError / OutputError / RegionError
    preflight.py         # RegionGuard (optional region/IP check)
    codex.py             # CodexLLM  (primary)
    claude.py            # ClaudeLLM (fallback)
    fallback.py          # FallbackLLM(primary, *fallbacks, on=(QuotaError,))
    cli.py               # minimal CLI: subllm complete / complete-json
    drivers/             # the isolated subprocess boundary (TS/Go portability seam)
      codex_exec.py      # contract for `codex exec`
      claude_tmux.py     # tmux driver for interactive `claude`
  docs/
    superpowers/specs/   # design spec (PRD)
    superpowers/plans/   # implementation plan
    research/            # background research + sourcing
    drivers-contract.md  # subprocess contract for future TS/Go ports
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
uv add /path/to/subllm
```

or pin it as a path source in the consumer's `pyproject.toml`:

```toml
[project]
dependencies = ["subllm"]

[tool.uv.sources]
subllm = { path = "/path/to/subllm" }
```

Editable install also works: `uv pip install -e /path/to/subllm`.

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

guard = RegionGuard(allowed_regions={"US", "JP"})   # checks public IP before calling
llm = CodexLLM(..., region_guard=guard)             # raises RegionError if out of region
```

`RegionError` is a hard stop and is **not** a default `FallbackLLM` trigger — if
you're out of region, every subscription client is equally blocked.

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

## API reference

| Export | Signature |
|---|---|
| `CodexLLM` | `(model=None, reasoning_effort="medium", search=False, codex_home=None, region_guard=None, attempts=3, timeout_s=120)` |
| `ClaudeLLM` | `(model=None, permission_mode="bypassPermissions", region_guard=None, attempts=3, timeout_s=300)` |
| `FallbackLLM` | `(primary, *fallbacks, on=(QuotaError,))` |
| `RegionGuard` | `(allowed_regions, lookup=default_lookup, ttl_s=300.0, on_lookup_failure="block")` |
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
