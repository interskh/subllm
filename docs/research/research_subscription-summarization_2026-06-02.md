# Running Summarization on Coding Subscriptions via Native Clients — Research (2026-06-02)

**Question:** Many current workflows (group-chat analysis, etc.) call the **Gemini API** just to do summarization / search-and-summarize. I already pay flat-rate for **Codex (ChatGPT plan)** and **Claude (Pro/Max)** coding subscriptions. Can I run those summarization jobs on the subscription quota instead of paying per-token for the Gemini API — using only **client-native** execution?

**Hard constraint:** No subscription-to-API proxies (claude2api, copilot-api, claude-code-proxy HTTP shims, etc.). They rewrite request shapes, aren't client-native, and carry account-ban risk. **The only acceptable approach is driving the genuine official clients headlessly:** `codex exec`, and the interactive `claude` TUI via tmux.

**Freshness filter:** Sources prioritized late-2025 / 2026. Primary docs (OpenAI Codex, Anthropic support) treated as canonical.

---

## Executive summary

1. **Strategy is validated by Anthropic's own docs.** The 2026-06-15 billing split is real and explicit: `claude -p` / Agent SDK / GitHub Actions draw from a **separate monthly programmatic credit** (Pro $20, Max 5x $100, Max 20x $200, billed at *full API rates*), while **"interactive Claude Code in the terminal or IDE" stays on the flat subscription**. So tmux-driving the *interactive* client is the only way to keep Claude summarization on plan quota.

2. **`codex exec` is the lower-friction path and probably the right default.** It is the *natively non-interactive* client (built for scripting); Codex has **no programmatic/interactive billing split at all** — `codex exec` under ChatGPT login is just "the plan." Clean stdout, `--output-schema`, native web search.

3. **The claude-tmux driver (ralph-loop) is a clever workaround for a client that doesn't want to be headless.** It's actually *more robust* than the community ecosystem (it tails the stream-json JSONL for `stop_reason` vs. their pane-scraping + idle heuristics) — but it's overkill for "summarize a chat blob."

4. **Recommendation:** Default to **`codex exec` (isolated, read-only)** for summarize / search-and-summarize. Keep the **claude-tmux driver** as a quality fallback and quota-overflow lane.

5. **Top gotcha is context pollution, not auth.** `codex exec` silently loads your `AGENTS.md` / `config.toml` / skills into the run — leaks coding persona into summaries *and* can mask extraction failures (it'll "summarize" empty input). Fix: isolated `CODEX_HOME`.

---

## 1. `codex exec` capabilities (verified vs current OpenAI docs)

`codex exec` is the documented non-interactive subcommand — purpose-built for scripting/automation.

**Auth — runs on the ChatGPT subscription, no API key.** Codex CLI is "included in your ChatGPT Free, Go, Plus, Pro, Business, Edu, or Enterprise plan"; ChatGPT sign-in draws from plan limits "at no extra charge." API-key sign-in is a separate usage-based path. → `codex exec` under ChatGPT login is genuinely on the flat subscription. (https://developers.openai.com/codex/pricing)

**Input handling:**
- Prompt as arg: `codex exec "summarize this"`
- Stdin as full prompt: `codex exec -`
- Prompt + piped context: pass a prompt arg AND pipe stdin → arg is the instruction, piped content is added context: `cat chat.txt | codex exec "Summarize this group chat in bullets"`
- No documented hard prompt-size cap in the exec docs (treat large transcripts as bounded by the model context window, not a CLI limit — *unverified*).
(https://developers.openai.com/codex/noninteractive)

**Output / structured output:**
- Default: only the final agent message → stdout; progress → stderr. (Key cleanliness win — redirect stdout, get just the summary.)
- `--json`: newline-delimited JSONL event stream (`thread.started`, `turn.*`, `item.*`, `error`).
- `-o, --output-last-message <path>`: write final message to file.
- `--output-schema ./schema.json`: constrain output to a JSON Schema — ideal for structured summaries (`{topics, action_items, ...}`).
(https://developers.openai.com/codex/noninteractive, https://developers.openai.com/codex/cli/reference)

**Suppressing agentic/tool behavior (clean summaries):**
- `--sandbox read-only` (`-s read-only`) — default; prevents file edits.
- `--skip-git-repo-check` — run outside a git repo.
- `--cd, -C <dir>` — set workspace root.
- No documented per-run flag to fully disable MCP/tools; suppress via `--ignore-user-config`, `--ignore-rules`, or a clean `CODEX_HOME`.
(https://developers.openai.com/codex/cli/reference)

**⚠️ Context pollution (the big gotcha).** `codex exec` loads your full Codex runtime context: `AGENTS.md`, `config.toml`, skills, personal instructions. Observed effects (from `steipete/summarize`): tone/personality leakage, lost source-grounding (summarizes even when zero input extracted), non-reproducible across machines. Fix: run isolated — `--ignore-user-config`, `--ignore-rules`, `--ephemeral`, and a temp `CODEX_HOME` containing only `auth.json`. (https://github.com/steipete/summarize/issues/215, https://github.com/steipete/summarize)

**Rate limits (subscription):** 5-hour windows + weekly caps. Per-window local-message ranges (pricing page): Plus ~15–80 (GPT-5.5) / ~20–100 (GPT-5.4); Pro 5x ≈ 5× Plus; Pro 20x ≈ 20× Plus; Business ≈ Plus. Small summaries consume a fraction of a "message" — cheap vs coding. (https://developers.openai.com/codex/pricing, https://github.com/openai/codex/discussions/2251)

**Concurrency:** *Undocumented.* No concurrency knob; multiple `codex exec` processes share the same quota with no parallelism guarantee. Serialize or test conservatively.

**Search:** Native. `--search` enables live web search for exec runs; config `web_search = "live" | "cached" | "disabled"` (cached is default, OpenAI-maintained index, lower prompt-injection risk). Works under ChatGPT auth. → "search-and-summarize" can be a single exec call. File search = reads workspace directly (read-only sandbox still allows reads). (https://developers.openai.com/codex/cli/features)

---

## 2. Claude interactive-via-tmux

### 2a. The 2026-06-15 `claude -p` billing change — CONFIRMED (Anthropic docs)

Anthropic's support article ("Use the Claude Agent SDK with your Claude plan") states the dedicated monthly programmatic credit covers:
- `claude -p` (non-interactive mode)
- Claude Agent SDK usage in your own projects
- Claude Code GitHub Actions and third-party Agent-SDK apps

And explicitly: **"Interactive Claude Code in the terminal or IDE" continues using standard subscription limits.** Programmatic usage is deducted from a separate credit pool at full API rates; credit drains first, then pay-as-you-go or requests halt.

Credit amounts: Pro $20, Max 5x $100, Max 20x $200, Team Standard $20/seat, Team Premium $100, Enterprise Premium $200.

Sources:
- https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan (authoritative)
- https://the-decoder.com/claude-subscriptions-get-separate-budgets-for-programmatic-use-billed-at-full-api-prices/
- https://www.digitalapplied.com/blog/anthropic-claude-credit-overhaul-june-15-2026

**Caveat (forward-looking, unverifiable):** Anthropic draws the line at "interactive in terminal/IDE" vs the `-p`/SDK code paths. A tmux-driven *interactive* session is still the genuine interactive binary → on the subscription side by the literal wording today. Anthropic *could* later detect automation patterns; no source indicates they currently do.

### 2b. Community projects driving the interactive Claude TUI (tmux/pty)

| Project | What it does | Completion detection | Maturity |
|---|---|---|---|
| **Dicklesworthstone/ntm** | Go; spawns/tiles/coordinates many Claude/Codex/Gemini agents across tmux panes; REST/WS API, approvals, "Agent Mail" | Idle detection (mechanism in `internal/`, undocumented; likely pane-idle) | Very active, v1.18.2 (May 2026). https://github.com/Dicklesworthstone/ntm |
| **absmartly/Tmux-Orchestrator** | 3-tier autonomous Claude agents, self-scheduling check-ins | `send-keys` literal mode + verification-retry (re-reads pane). Pane-scraping. | Established, widely forked. https://github.com/absmartly/Tmux-Orchestrator |
| **primeline-ai/claude-tmux-orchestration** | Parallel Claude workers + heartbeat + file-based coordination | Borrows send-keys + idle detection from ntm. Heartbeat = pane capture. | Active, smaller. https://github.com/primeline-ai/claude-tmux-orchestration |
| **adamwulf ittybitty** | Claude in tmux virtual terminal; agents spawn sub-agents | Pane/PTY based | Blog-stage, Jan 2026. https://adamwulf.me/2026/01/itty-bitty-ai-agent-orchestrator/ |
| **seunggabi/claude-dashboard** | k9s-style TUI to manage Claude sessions via tmux | Monitoring, not turn-detection | Active. https://github.com/seunggabi/claude-dashboard |
| **njbrake/agent-of-empires** | Manage many Claude/Codex/Gemini agents from TUI/Web/mobile | Multi-client wrapper | Active. https://github.com/njbrake/agent-of-empires |

**Gap:** most orchestrators rely on **pane scraping + idle heuristics**, NOT JSONL transcript parsing. The ralph-loop driver (JSONL stream-json tailing for `stop_reason`) is *more robust* than the mainstream ecosystem.

### 2c. Completion-detection techniques (ranked by reliability)

1. **JSONL session-transcript tailing (most reliable — what ralph-loop already does).** Claude Code appends one JSON object/line to `~/.claude/projects/<url-encoded-cwd>/...<uuid>.jsonl` (honors `CLAUDE_CONFIG_DIR`). Watch for `"type":"assistant"` records with a terminal `stop_reason` (`end_turn`, or `tool_use` before a tool call). Gotcha: streaming writes partial assistant lines with `stop_reason: null` / `output_tokens: 1` before the consolidated line — only trust entries with `stop_reason` set; merge consecutive assistant entries. Known bug: under `--output-format stream-json`, `SESSION_CLOSE` may hit the JSONL file but not stdout — **tail the file, not stdout.** (https://medium.com/@ywian/what-i-learned-parsing-claude-codes-jsonl-session-logs-268248be0a2c, https://databunny.medium.com/inside-claude-code-the-session-file-format-and-how-to-inspect-it-b9998e66d56b, https://github.com/anthropics/claude-code/issues/17248)
2. **Sentinel / echo markers.** Tell Claude to end every response with a unique token (`<<<DONE-7f3a>>>`); grep pane/transcript. Robust vs streaming partials; weakness: model occasionally forgets.
3. **tmux `pipe-pane` logging.** `tmux pipe-pane -t <pane> 'cat >> /tmp/pane.log'` — continuous log for stuck-detection/heartbeats.
4. **Pane-idle heuristic.** Diff `capture-pane -p` over an interval; unchanged for N sec + prompt back → done. Least reliable (false positives on slow tool runs). Best kept as heartbeat/stuck-detector only — which is how ralph-loop uses it.

---

## 3. Head-to-head for summarization / search-and-summarize

| Dimension | `codex exec` (ChatGPT sub) | claude interactive via tmux |
|---|---|---|
| Setup complexity | **Low** — one subprocess, stdin in, clean stdout | High — spawn tmux, send-keys, tail JSONL, handle banner/MOTD, parse stop_reason |
| Batch / unattended robustness | **High** — deterministic exit, `-o` to file | Medium — interactive client not meant to be headless; needs idle/stuck detection, warm-up |
| Latency / throughput per job | **Fast** — no TUI warm-up | Slower — TUI boot/banner/model load per ephemeral session; better with a warm pane |
| Output cleanliness | **Very clean** — stdout = final message (isolate CODEX_HOME) | Noisier — extract final assistant text from JSONL |
| Structured output | **Native** — `--output-schema`, `--json` | None native — prompt JSON, parse out |
| Search-in-loop | **Native** — `--search` / `web_search` config | Works but needs pre-granted tool perms or it deadlocks on approval |
| Subscription-quota friendliness | **Clean** — no programmatic/interactive split exists | Clean ONLY via interactive tmux; `claude -p` hits the new credit |
| Concurrency | Undocumented; processes share quota | One pane = one session; parallelism via multiple panes |

**Lowest-friction for a pure "summarize this chat blob": `codex exec`, decisively.** claude-tmux is more moving parts for the same outcome; its edge is only relevant if you need Claude's summarization style/quality or you amortize a warm pane across many prompts.

---

## 4. Practical recommendation

**Default to `codex exec` (isolated, read-only) for summarize / search-and-summarize. Keep the claude-tmux driver as quality fallback / quota-overflow.**

Why codex first:
1. Natively non-interactive — the tmux driver is a workaround for a client that fundamentally doesn't want to be headless. Don't pay that complexity tax for a one-subprocess task.
2. Cleaner quota story — no programmatic/interactive split to police (claude is only clean if you stay strictly on interactive-tmux).
3. Native `--output-schema` + built-in search fit "search-and-summarize → structured result."

Use the claude driver when:
- You prefer Claude's summarization quality/voice for Chinese chat content (A/B worth testing — coding models can be uneven on non-English social text).
- A warm claude pane is already running and you can piggyback prompts.
- You hit Codex's 5-hour/weekly caps and want to spread load across both subscriptions.

**Concrete codex recipe (isolated, clean):**
```bash
CODEX_HOME=/tmp/codex-clean \      # contains only auth.json (copy real one once)
codex exec -s read-only --skip-git-repo-check \
  --ignore-user-config --ignore-rules \
  -o /tmp/summary.txt \
  "Summarize this group chat: key topics, decisions, action items." < chat.txt
```

**Gotchas to plan for:**
- *Codex context pollution*: isolate `CODEX_HOME` or your coding `AGENTS.md` leaks in — also silently masks extraction failures (summarizes empty input).
- *Codex JSON trajectory*: open ask for a flag to save full trajectory JSON for exec (issue #2288); `--json` gives events but persistence ergonomics are rough.
- *Codex concurrency*: undocumented — serialize or test before parallelizing.
- *Claude tmux warm-up*: per-call TUI/banner/model-load overhead is real; for many small summaries keep a warm pane and feed sequential prompts, or just use codex.
- *Claude tool approvals*: pre-grant WebSearch/file perms in session config or an unattended loop deadlocks on the approval prompt.
- *Claude JSONL stdout bug*: tail the file, not stdout, for `SESSION_CLOSE` (issue #17248).

---

## Key sources
- Codex non-interactive: https://developers.openai.com/codex/noninteractive
- Codex CLI reference: https://developers.openai.com/codex/cli/reference
- Codex features (web search): https://developers.openai.com/codex/cli/features
- Codex pricing / subscription auth + limits: https://developers.openai.com/codex/pricing
- Codex usage limits discussion: https://github.com/openai/codex/discussions/2251
- Codex exec JSON trajectory ask: https://github.com/openai/codex/issues/2288
- Summarize context-pollution + isolation workaround: https://github.com/steipete/summarize/issues/215 ; https://github.com/steipete/summarize
- Anthropic Agent SDK / billing (authoritative): https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan
- Billing-change corroboration: https://the-decoder.com/claude-subscriptions-get-separate-budgets-for-programmatic-use-billed-at-full-api-prices/ ; https://www.digitalapplied.com/blog/anthropic-claude-credit-overhaul-june-15-2026
- Claude JSONL format / completion detection: https://databunny.medium.com/inside-claude-code-the-session-file-format-and-how-to-inspect-it-b9998e66d56b ; https://medium.com/@ywian/what-i-learned-parsing-claude-codes-jsonl-session-logs-268248be0a2c
- stream-json SESSION_CLOSE bug: https://github.com/anthropics/claude-code/issues/17248
- tmux orchestrators: https://github.com/Dicklesworthstone/ntm ; https://github.com/absmartly/Tmux-Orchestrator ; https://github.com/primeline-ai/claude-tmux-orchestration ; https://github.com/seunggabi/claude-dashboard ; https://github.com/njbrake/agent-of-empires ; https://adamwulf.me/2026/01/itty-bitty-ai-agent-orchestrator/

**Confidence:** High on codex exec mechanics, subscription auth, and the June-15 billing change (primary docs). Medium on exact codex rate-limit numbers (vary by task; ranges only). Flagged/low: codex exec concurrency (undocumented); forward-looking enforcement of the interactive-vs-programmatic line for tmux-driven Claude (literal policy favors it today; no future guarantee).
