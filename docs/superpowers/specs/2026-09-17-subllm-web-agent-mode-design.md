# subllm web-agent mode — Design Spec (2026-09-17)

**Consumer:** w2agent P4-2 outcome-calibration pilot, `/Volumes/Ext1TB/Projects/w2agent-platform/docs/plans/2026-09-17-p4-2-outcome-calibration.md` (spec-reviewed). Its §3 D-3 names this change as the prerequisite for its slice 1. Read that doc's §3 D-1, D-3 and D-6 before implementing; this spec serves them.
**Repo:** `~/Projects/subllm` (`python/` is authoritative). The TS SDK is not touched.
**Hard constraint (unchanged):** client-native execution only; no subscription-to-API proxy.

## 1. Problem & goals

subllm today runs text-in/text-out turns. The Codex path can switch on web search but returns only the final message. The Claude path always runs with every tool disabled, so it cannot browse. Neither says what the agent did.

The w2agent pilot needs an **agent that browses the public web and nothing else**, on both subscriptions, with a record of what it searched and which pages it actually retrieved. Two properties are load-bearing there:

- **Grounding.** A task earns credit only when the page the agent cites appears in its own record of retrieved pages. Without a trustworthy trace, an answer from memory looks the same as an answer from the site.
- **Isolation.** The agent reads untrusted web pages (prompt injection is expected) on the owner's Mac, which also holds reference answers and production credentials. The agent must have no shell, no file access, no other tools, and no project secrets in its environment, and the two clients must be held to the same contract.

Goals, with the evidence that shows each is met:

- **G1. One web-agent call per client** returns the final answer (text, or schema-validated JSON) plus a trace, for `codex` and `claude`, with the model chosen by the caller.
  - **Evidence:** opt-in live test per client: ask for the title of a stable public page; the result's answer matches, and the trace lists that page as retrieved.
- **G2. A uniform trace.** Both clients produce the same trace shape: ordered search and fetch events, with a derived list of retrieved URLs.
  - **Evidence:** hermetic parser tests run on recorded live captures from each client (committed fixtures), including a failed fetch, which is not counted as retrieved.
- **G3. Isolation holds, and fails closed.** No shell, file or other tool is available; the process environment holds only an allowlist; the client configuration is isolated. Any tool event outside the allowed set makes the call fail loudly, never return an answer.
  - **Evidence:** opt-in live negative control per client: the prompt asks the agent to list the working directory and read a canary file placed outside it. The canary's contents never appear in the answer, and no non-web tool event appears in the trace. Hermetic test: a fixture containing a non-web tool event raises the isolation error.
- **G4. CLI access.** A command-line entry point runs a web-agent call and prints one JSON document on stdout, so the TypeScript harness in w2agent can call it as a subprocess.
  - **Evidence:** hermetic CLI test with stubbed binaries: the output parses against the §4 contract, with a non-zero exit and a typed error on quota, client failure and isolation violation.

## 2. Non-goals

- **Existing calls do not change.** `complete` / `complete_json` / `complete_json_schema` keep their current behaviour and defaults: Claude with all tools off, Codex without search unless asked.
- **No w2agent logic.** No beacon, canary, nonce, vantage or grading logic lives in subllm. The trace is generic.
- **No TS SDK parity.** Porting web-agent mode to `ts/` is deferred.
- **No browser, computer-use or MCP tools**, even though both clients offer them.
- **No egress claim.** subllm reports what the client says it did. Where the fetch physically ran is measured by the consumer, not asserted here.

## 3. Architecture & decisions

### D-1 A separate web-agent call with a richer result, not flags on `complete`

**Selected:** each client gains a web-agent method returning a result object (answer + trace + client + model), and the CLI gains a matching subcommand. The existing methods are untouched.

**Rejected: turning on search/tools through constructor flags on the existing clients.** The existing methods return plain text and cannot carry a trace. Silently widening what a `ClaudeLLM` may do based on a flag would also weaken the "tools off by default" safety that current consumers rely on.

### D-2 Codex: web search on, every other capability disabled, JSONL events as the trace

**Selected:** the Codex driver in web-agent mode:

- keeps the current isolation: throwaway `CODEX_HOME` with only `auth.json`, ignored user config and rules, ephemeral session, project docs disabled, a read-only sandbox;
- adds an empty temporary working directory for the process, closing the known gap in the drivers contract;
- enables live web search;
- **disables every non-web tool capability through the client's feature switches**: the shell and unified-exec tools, browser use, computer use and apps/connectors. As of codex-cli 0.154.0 these appear in `codex features list` as `shell_tool`, `unified_exec`, `browser_use`, `computer_use` and `apps`. The implementer re-verifies the names against the installed version;
- **fails closed:** before running, confirms every feature it intends to disable exists in the installed client. A missing or renamed feature raises a client error instead of running with an unknown tool surface;
- requests JSONL events and derives the trace from them, while the final answer still comes from the output file (and output schema, when given).

The JSONL event shapes for web search (search queries, page opens, in-page finds) are not documented in this repo. The implementer **records a live capture first**, commits it as a fixture, and writes the parser against it. Unknown event types are kept raw in the trace, not dropped.

**Rejected: relying on the read-only sandbox alone.** A read-only sandbox still lets the model run shell commands that read any file on the machine, which is exactly the exposure the consumer must rule out.

### D-3 Claude: web tools only, isolated configuration, transcript as the trace

**Selected:** the Claude driver in web-agent mode:

- limits the built-in tool set to web fetch and web search, and loads no MCP servers;
- ignores user, project and local settings files, so hooks, which run shell commands, cannot fire, and neither can permissions or memory from the owner's setup. The installed client offers restricted mode plus strict MCP configuration for this; the implementer verifies which flags achieve it on the installed version;
- keeps the owner's global instruction files out of the session. If the client cannot exclude them while still authenticating from the owner's login, the implementer documents the gap and fails closed rather than silently running with them;
- runs in an empty temporary working directory;
- keeps bypass-permissions mode, which is safe only because no tool beyond web fetch and search exists; the tmux, readiness, paste and completion mechanics stay as today;
- derives the trace from the session transcript: tool-use blocks give the query or URL, and the paired tool results give success or error.

**Rejected: `claude -p`.** It bills the separate programmatic credit after 2026-06-15 (v1 design). Interactive over tmux stays.

### D-4 One trace contract; "retrieved" means a successful fetch

- A **search** event records the query, and the result URLs when the client exposes them. Search results are *not* retrieved pages.
- A **fetch** event records the URL, and whether the client reports success. A page open or in-page find that returns content counts as a fetch for Codex. Claude's web fetch counts.
- **Retrieved URLs** = the URLs of successful fetch events, deduplicated, in order of first retrieval. Consumers ground answers against this list only.
- Any **other tool event** is recorded as-is and triggers D-5.

### D-5 Isolation violations fail loudly

If the trace contains any event that is not a web search or web fetch, the call raises a dedicated isolation error carrying the trace. The CLI exits non-zero with that error type. The answer is never returned alongside a violation.

### D-6 Environment allowlist for both clients

Web-agent calls pass only an explicit allowlist into the child process: PATH, HOME, USER, LANG, TERM, the client's config-home variable, and whatever the client strictly needs to authenticate (verified per client). Everything else in the caller's environment, including database URLs and API secrets, is dropped. Today the Codex driver passes the full environment. That is fixed for web-agent mode only, so existing calls keep their behaviour.

## 4. Data contracts

### 4.1 Python

```python
@dataclass(frozen=True)
class TraceEvent:
    kind: Literal["search", "fetch", "other"]
    query: str | None = None          # search
    url: str | None = None            # fetch
    ok: bool | None = None            # fetch: client-reported success
    result_urls: tuple[str, ...] = () # search, when exposed
    raw_type: str | None = None       # client's own event/tool name, always set

@dataclass(frozen=True)
class WebAgentResult:
    client: Literal["codex", "claude"]
    model: str | None
    answer: str | dict                # dict when a schema was given
    trace: tuple[TraceEvent, ...]
    retrieved_urls: tuple[str, ...]   # successful fetches, dedup, first-seen order
    duration_ms: int

class IsolationError(SubllmError):   # carries .trace
    ...
```

Client method: `run_web_task(prompt, schema_model=None, *, timeout_s=...) -> WebAgentResult`. The model comes from the client constructor as today. `QuotaError` / `ClientError` / `OutputError` keep their current meanings; `IsolationError` is new and is **not** a `FallbackLLM` trigger.

### 4.2 CLI

```
subllm web-agent --client codex|claude --model M [--schema schema.json] [--timeout S] [PROMPT|-]
```

stdout on success, exit 0:

```json
{"client":"codex","model":"…","answer":"…" ,
 "trace":[{"kind":"search","query":"…","result_urls":[],"raw_type":"…"},
          {"kind":"fetch","url":"https://…","ok":true,"raw_type":"…"}],
 "retrieved_urls":["https://…"],"duration_ms":12345}
```

On failure: non-zero exit, and stdout holds `{"error": "quota"|"client"|"output"|"isolation", "message": "…", "trace": [...] | null}` so the consumer never parses stderr.

## 5. Slice plan

| # | slice (user-visible proof) | tasks | subsystems | risk (closed list) | depends on |
|---|---|---|---|---|---|
| 1 | **Codex web agent.** `subllm web-agent --client codex --model gpt-5.6-luna "Open https://example.com and give its title"` prints the §4.2 JSON with `example.com` in `retrieved_urls` | T1 shared trace types, `IsolationError`, the CLI subcommand and its JSON error contract. T2 Codex driver web-agent mode (D-2, D-6): feature disables with a fail-closed existence check, empty working directory, JSONL trace parser written from a committed live capture; hermetic tests (fixture parse, failed fetch not retrieved, non-web event → isolation error, missing feature → client error, env allowlist); opt-in live tests: G1 and the G3 negative control | drivers + cli | security | — |
| 2 | **Claude web agent.** Same command with `--client claude --model claude-haiku-4-5-20251001` prints the same shape | T1 Claude driver web-agent mode (D-3, D-6): web-only tool set, settings/MCP/instruction isolation with fail-closed on an unexcludable source, transcript trace parser from a committed live capture; hermetic tests as in slice 1; opt-in live G1 and G3. T2 docs: drivers contract (both modes), README usage, AGENTS.md note | drivers + docs | security | 1 |

Parallel eligibility: none (slice 2 reuses slice 1's trace types and CLI).

**Owner prerequisites:** `codex` logged in to the ChatGPT plan; `claude` logged in to Max, plus `tmux`; live tests run with `SUBLLM_LIVE=1`.

## 6. Assumed decisions

- **Python only.** The consumer calls the CLI as a subprocess, so the TS SDK needs no change now.
- **Feature names are verified at run time, not trusted from this doc.** CLIs rename features between versions, and a silently ignored "disable" would reopen the shell.
- **Fixtures come from live captures**, committed with the client version that produced them.
- **The canary file in the negative control lives in a temp directory outside the working directory**, with random contents, so a hit cannot be a coincidence.
- **Bypass-permissions stays on for Claude** in web-agent mode, which is safe only because the tool set is web-only. Changing either one requires changing both.

## 7. Deferred hardening

- TS SDK parity for web-agent mode.
- Streaming trace events to the caller during long turns.
- A per-call URL allow/deny list enforced by subllm (today the consumer decides from the trace after the fact).
- Recording fetch response metadata (status, bytes) when a client exposes it.
- Detecting a client upgrade that changes event shapes, e.g. by failing when zero web events parse from a run that returned an answer.

## 8. Review status

spec-review: pending
