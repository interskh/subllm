# subllm — agent handoff

**Polyglot repo:** `python/` is the authoritative full-featured implementation
(CodexLLM + ClaudeLLM + FallbackLLM + CLI); `ts/` is a phase-1 `CodexLLM` SDK
targeted at `your-app`. Both share `docs/drivers-contract.md` as the
cross-language subprocess contract.

You are implementing **subllm**: a Python library that runs LLM requests
(summarize / search-and-summarize) through flat-rate coding subscriptions via the
**genuine official clients** — `codex exec`, interactive `claude` driven over
tmux, and the official chatgpt.com web app driven in a real logged-in browser
through that browser's own automation interface. No per-token API. No proxy.

## Read first (in order)

1. `docs/superpowers/specs/2026-06-02-subllm-v1-design.md` — the approved design spec. This is authoritative.
2. `docs/superpowers/plans/2026-06-02-subllm-v1.md` — the ordered implementation plan. Execute it step by step.
3. `docs/research/research_subscription-summarization_2026-06-02.md` — background,
   sourcing, and the gotchas (codex context pollution; claude 2026-06-15 billing
   split; JSONL completion detection).

## Hard constraint (non-negotiable)

Client-native execution only. NEVER build or use a subscription-to-API HTTP proxy
(token wrapped behind an OpenAI-compatible endpoint) — ban risk. subllm only
drives the real official CLIs as subprocesses, plus the official chatgpt.com web
app in a real logged-in browser via that browser's own automation interface.
Requests go only through the page's own UI: no HTTP server in subllm and no
`backend-api` replay.

## Web-agent mode (shipped, Python only)

Beyond the text calls, both clients have `run_web_task` (+ `subllm web-agent`):
one isolated browsing turn returning the answer plus a trace of what was
searched and retrieved. Spec: `docs/superpowers/specs/2026-09-17-subllm-web-agent-mode-design.md`;
contract: `docs/drivers-contract.md`.

- The isolation boundary is `python/src/subllm/drivers/{codex,claude}_web.py`
  plus `drivers/egress.py`. Change nothing there without re-reading D-3a/D-4b.
- The hard constraint above is intact. `egress.py` is a network-layer CONNECT
  filter (claude only): it terminates no TLS, reads/rewrites no token or
  request, and speaks no vendor API — the client authenticates to Anthropic
  itself. It is not a subscription-to-API proxy.
- **A client update blocks web-agent mode** until the live G3 negative control
  is re-run and the new version (codex: version+model) is added to the driver's
  admitted set. Fail-closed by design; do not widen the set on faith.

## Build order (see docs/superpowers/plans/2026-06-02-subllm-v1.md for detail)

1. `base.py`, `errors.py`, `preflight.py` (RegionGuard), `DryRunLLM`, pyproject/uv, tests scaffold
2. `CodexLLM` end-to-end (ship first)
3. minimal CLI
4. `ClaudeLLM` via adapted tmux driver
5. `FallbackLLM` + drivers-contract docs

Tooling: `uv` for venv/deps. Tests use stub `codex`/`tmux` binaries on PATH — no
real subscription calls in tests. The one exception is the opt-in live smoke
suite (`tests/test_integration.py`, and `tests/test_integration_web_agent.py`
for web-agent mode), skipped unless `SUBLLM_LIVE=1`; run it with
`SUBLLM_LIVE=1 uv run pytest -m integration` to verify the clients against the
real subscriptions.
