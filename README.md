# subllm

Run LLM requests (summarize / search-and-summarize) through your **flat-rate
coding subscriptions** (Codex/ChatGPT plan, Claude Pro/Max) via the **genuine
official clients** — no per-token API billing, no request-rewriting proxy.

Drop-in for a `BaseLLM` seam: `complete` / `complete_json` / `complete_json_schema`.

**Status:** v1 implemented (CodexLLM + ClaudeLLM + FallbackLLM + CLI).

- Design spec: [`docs/design.md`](docs/design.md)
- Background research: [`docs/research/`](docs/research/)

## Hard constraint

Client-native execution only. No subscription-to-API HTTP proxies (ban risk).
subllm only drives the real official CLIs (`codex exec`, interactive `claude`
via tmux) as subprocesses.
