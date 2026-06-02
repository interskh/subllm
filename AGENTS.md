# subllm — agent handoff

**Polyglot repo:** `python/` is the authoritative full-featured implementation
(CodexLLM + ClaudeLLM + FallbackLLM + CLI); `ts/` is a phase-1 `CodexLLM` SDK
targeted at `your-app`. Both share `docs/drivers-contract.md` as the
cross-language subprocess contract.

You are implementing **subllm**: a Python library that runs LLM requests
(summarize / search-and-summarize) through flat-rate coding subscriptions via the
**genuine official clients** — `codex exec` and interactive `claude` driven over
tmux. No per-token API. No proxy.

## Read first (in order)

1. `docs/superpowers/specs/2026-06-02-subllm-v1-design.md` — the approved design spec. This is authoritative.
2. `docs/superpowers/plans/2026-06-02-subllm-v1.md` — the ordered implementation plan. Execute it step by step.
3. `docs/research/research_subscription-summarization_2026-06-02.md` — background,
   sourcing, and the gotchas (codex context pollution; claude 2026-06-15 billing
   split; JSONL completion detection).

## Hard constraint (non-negotiable)

Client-native execution only. NEVER build or use a subscription-to-API HTTP proxy
(token wrapped behind an OpenAI-compatible endpoint) — ban risk. subllm only
drives the real official CLIs as subprocesses.

## Build order (see docs/superpowers/plans/2026-06-02-subllm-v1.md for detail)

1. `base.py`, `errors.py`, `preflight.py` (RegionGuard), `DryRunLLM`, pyproject/uv, tests scaffold
2. `CodexLLM` end-to-end (ship first)
3. minimal CLI
4. `ClaudeLLM` via adapted tmux driver
5. `FallbackLLM` + drivers-contract docs

Tooling: `uv` for venv/deps. Tests use stub `codex`/`tmux` binaries on PATH — no
real subscription calls in tests.
