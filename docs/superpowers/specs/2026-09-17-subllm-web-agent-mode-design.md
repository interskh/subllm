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
  - **Evidence:** opt-in live negative control per client. The prompt asks the agent to list its working directory, read a canary file placed outside it (by path and as a local-file URL through its fetch tool), fetch a loopback address served by the test (directly and through a public-looking name that resolves to loopback), and report a sentinel environment variable set in the caller. None of the canary contents, the loopback response or the sentinel value appears in the answer; the loopback server records no request; and no non-web tool event appears in the trace. Hermetic test: a fixture containing a non-web tool event raises the isolation error.
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
- **refuses to run on an unexpected feature inventory:** before running, it reads the installed client's feature inventory with the web-agent overrides applied. A feature it intends to disable that does not exist, or any enabled feature not on an allowlist of features known to add no callable tool, raises a client error instead of running. This narrows the surface and catches renamed switches; it is not by itself proof that the surface is closed, which D-3a requires;
- requests JSONL events and derives the trace from them, while the final answer still comes from the output file (and output schema, when given).

The JSONL event shapes for web search (search queries, page opens, in-page finds) are not documented in this repo. The implementer **records a live capture first**, commits it as a fixture, and writes the parser against it. Unknown event types are kept raw in the trace, not dropped.

**Rejected: relying on the read-only sandbox alone.** A read-only sandbox still lets the model run shell commands that read any file on the machine, which is exactly the exposure the consumer must rule out.

### D-3 Claude: web tools only, isolated configuration, transcript as the trace

**Selected:** the Claude driver in web-agent mode:

- limits the built-in tool set to web fetch and web search, and loads no MCP servers;
- ignores user, project and local settings files, so hooks, which run shell commands, cannot fire, and neither can permissions or memory from the owner's setup. The installed client offers restricted mode plus strict MCP configuration for this; the implementer verifies which flags achieve it on the installed version;
- runs from a **dedicated configuration home that holds only what authentication needs**, as the Codex driver already does, never the owner's real one. The owner's global instructions, plugins, skills, agents, history and transcripts therefore cannot enter the session. If the client cannot authenticate from such a home (for example, credentials that are bound to the owner's home), the owner logs in once into the dedicated home; if that is impossible, Claude is not admitted to web-agent mode;
- runs in an empty temporary working directory;
- keeps bypass-permissions mode, so no prompt can stall the session. This is safe only because the offered tool set is proven web-only (D-3a) and non-public targets are blocked by a mechanism that holds under this mode (D-4b). Permission deny rules count as that mechanism only if the live negative control shows them denying under bypass-permissions; the tmux, readiness, paste and completion mechanics stay as today;
- derives the trace from the session transcript: tool-use blocks give the query or URL, and the paired tool results give success or error.

**Rejected: `claude -p`.** It bills the separate programmatic credit after 2026-06-15 (v1 design). Interactive over tmux stays.

### D-3a Admission requires positive proof of the offered tool set (both clients)

Switch settings describe what should be off; they do not prove what the model was offered. A client update can add an always-available tool that no switch names, and catching its use afterwards is too late on a machine holding secrets. Admission is therefore by positive evidence:

- **Where the client reports the tools it offered the model** (for example in a session-start record), the driver checks that report against the allowed set (web search and web fetch, or nothing for *none*) before the model acts on untrusted content, and fails closed on any difference or a missing report.
- **Where the client reports no such inventory**, a client version is admitted only after it has passed the live G3 negative control. The driver records the admitted versions and refuses to run on any other, so a client update blocks web-agent mode until the control is re-run.

**The isolation boundary is this capability set, not an operating-system sandbox.** The claim "no file access" rests on the agent being offered no file-, shell- or code-running tool (this section) and on its fetch tool reaching only public targets (D-4b). Configuration isolation (D-2, D-3) and the environment allowlist (D-6) keep secrets and owner instructions out of the session; they are not what stops file access. Codex additionally keeps its read-only sandbox; an OS-level filesystem sandbox for the Claude process is deferred (§7).

### D-4 One trace contract; "retrieved" means a successful fetch

- A **search** event records the query, and the result URLs when the client exposes them. Search results are *not* retrieved pages.
- A **fetch** event records the URL, and whether the client reports success. A page open or in-page find that returns content counts as a fetch for Codex. Claude's web fetch counts.
- **Retrieved URLs** = the URLs of successful fetch events, deduplicated, in order of first retrieval. Consumers ground answers against this list only.
- Any **other tool event** is recorded as-is and triggers D-5.
- **A fetch has two addresses.** The trace records the requested URL and, where the client exposes it, the final URL after redirects. The retrieved list uses the final URL; a fetch whose final URL is unknown is marked so, and consumers must not ground a site against a requested address alone.
- **Trace fidelity is declared, never guessed.** A client may not expose which pages its hosted search actually opened (for example, it may report only queries). The live capture decides this per client, and every result states whether fetch events are exposed. When they are not, the retrieved list is empty and the result says so. It is never reconstructed from URLs the model cites in its answer, because that would make grounding circular. **For this spec that is a delivery failure, not a quiet mode:** G1 requires the retrieved page to appear in the trace, so a client whose capture shows no fetch events leaves its slice blocked and surfaced to the owner rather than shipped.
- **Trace completeness is a validity state, and unknown evidence fails closed.** A result is marked complete only when every event in the client's stream was recognised, and every tool call is paired with its result. An unrecognised record that is, or may be, a tool invocation is an isolation violation (D-5), not an incomplete trace. Any other unrecognised, unpaired or truncated evidence marks the trace incomplete, and so does an answer with zero web events on a web-tool call, which catches a client update that changes event shapes. **An incomplete trace yields an empty retrieved list by construction**, so no consumer can ground against it by accident, and the consumer must treat such a run as a delivery failure, not as an ungrounded answer.
- **Raw evidence is preserved.** Every trace event carries the client's original record alongside its classification, so an isolation failure or an unrecognised event can be diagnosed from the result itself.

### D-4a One attempt per call

Web-agent calls do not auto-retry, unlike the existing text calls. A retry would re-run a browsing session invisibly: the consumer's timing, its vantage checks and its failure accounting would all describe an attempt that is not the one returned. Every failure surfaces to the caller, who decides whether to run again. The optional region preflight applies as it does to other calls.

### D-4b Web access is limited to public web targets

The web tools themselves are a path to local resources: a fetch of a local-file URL, the loopback interface, a link-local or private-network address, or a public URL that redirects to one could reach a dev server, a metadata endpoint or a LAN service without any forbidden tool being used. Web-agent mode therefore restricts fetch targets to public `http`/`https` hosts:

- **"Public" is judged on where a connection actually goes, not on how the URL reads.** A public-looking name can resolve to a loopback or private address, and a public page can redirect to one. The invariant is: every network destination the fetch connects to, after name resolution, and every redirect hop, is a public address reached over `http`/`https`.
- **Where a client's fetch may execute on the local machine** (Claude's fetch tool, until proven otherwise), that invariant is enforced **before** each connection. URL-pattern permission rules alone do not satisfy it. The mechanism is the implementer's choice (a client-native control that covers resolution and redirects, or a subllm-run egress filter the client process is confined to), but it must hold under the permission mode in use (D-3), and the live negative control must show it blocking a local-file URL, a loopback address, and a public-looking name that resolves to loopback. If no mechanism passes, that client is not usable in web-agent mode and the call fails closed.
- **For every client**, any traced fetch whose requested or final target falls in those classes is an isolation violation (D-5), including a public URL whose final target is not public.
- Hosted search that runs on the vendor's servers cannot reach the owner's local network, but it is held to the same after-the-fact check.

### D-4c An isolated no-tools call for grading and other untrusted text

The consumer's grader reads agent answers, which are derived from untrusted web content, so it needs the same isolation with **no** tools at all. The existing text calls do not provide that: they run with the caller's full environment, the owner's client configuration, and, for Codex, a shell. Web-agent mode therefore has a tool setting of *web* or *none*. With *none*, the call gets every isolation guarantee in this spec (proven tool set, here empty; environment allowlist, dedicated multiplexer server, isolated configuration, empty working directory), an empty trace, and any traced tool event is a violation. The existing text calls are unchanged.

### D-5 Isolation violations fail loudly

If the trace contains any event that is not a web search or web fetch, the call raises a dedicated isolation error carrying the trace. The CLI exits non-zero with that error type. The answer is never returned alongside a violation.

### D-6 Environment allowlist for both clients

Web-agent calls pass only an explicit allowlist into the child process: PATH, HOME, USER, LANG, TERM, the client's config-home variable, and whatever the client strictly needs to authenticate (verified per client). Everything else in the caller's environment, including database URLs and API secrets, is dropped. Today the Codex driver passes the full environment. That is fixed for web-agent mode only, so existing calls keep their behaviour.

**The allowlist must hold for the process that actually runs the agent, not just the process subllm spawns.** For Claude, the agent runs inside a terminal-multiplexer session. A session inherits the global environment of whichever multiplexer server is already running, and that server may have been started from the owner's shell with every secret loaded. Web-agent mode therefore runs Claude on a dedicated multiplexer server of its own, started from the allowlisted environment and torn down after the call, never on a shared server. The G3 negative control also checks that a sentinel variable set in the caller's environment is invisible to the agent.

## 4. Data contracts

### 4.1 Python

```python
@dataclass(frozen=True)
class TraceEvent:
    kind: Literal["search", "fetch", "other"]
    query: str | None = None          # search
    url: str | None = None            # fetch: requested URL
    final_url: str | None = None      # fetch: after redirects, when exposed
    ok: bool | None = None            # fetch: client-reported success
    result_urls: tuple[str, ...] = () # search, when exposed
    raw_type: str                     # client's own event/tool name
    raw: dict                         # the client's original record(s), unmodified

@dataclass(frozen=True)
class WebAgentResult:
    client: Literal["codex", "claude"]
    model: str | None
    answer: str | dict                # dict when a schema was given
    trace: tuple[TraceEvent, ...]
    retrieved_urls: tuple[str, ...]   # successful fetches, dedup, first-seen order
    fetch_events_exposed: bool        # False → retrieved_urls is empty by construction
    trace_complete: bool              # False → retrieved_urls is empty; consumers treat the run as a delivery failure
    tools: Literal["web", "none"]
    client_version: str               # the CLI version that produced the trace
    duration_ms: int

class IsolationError(SubllmError):   # carries .trace
    ...
```

Client method: `run_web_task(prompt, schema_model=None, *, tools="web", timeout_s=...) -> WebAgentResult`, with `tools="none"` for the isolated no-tools call (D-4c). The model comes from the client constructor as today. `QuotaError` / `ClientError` / `OutputError` keep their current meanings; `IsolationError` is new and is **not** a `FallbackLLM` trigger.

### 4.2 CLI

```
subllm web-agent --client codex|claude --model M [--tools web|none] [--schema schema.json] [--timeout S] [PROMPT|-]
```

stdout on success, exit 0:

```json
{"client":"codex","model":"…","tools":"web","answer":"…" ,
 "trace":[{"kind":"search","query":"…","result_urls":[],"raw_type":"…","raw":{}},
          {"kind":"fetch","url":"https://…","final_url":"https://…","ok":true,"raw_type":"…","raw":{}}],
 "retrieved_urls":["https://…"],"fetch_events_exposed":true,"trace_complete":true,
 "client_version":"…","duration_ms":12345}
```

On failure: non-zero exit, and stdout holds `{"error": "quota"|"client"|"output"|"isolation"|"region", "message": "…", "trace": [...] | null}` so the consumer never parses stderr. This deliberately differs from the existing subcommands, which print errors to stderr: a machine consumer needs a typed error and the partial trace, and the existing subcommands keep their behaviour.

## 5. Slice plan

| # | slice (user-visible proof) | tasks | subsystems | risk (closed list) | depends on |
|---|---|---|---|---|---|
| 1 | **Codex web agent.** `subllm web-agent --client codex --model gpt-5.6-luna "Open https://example.com and give its title"` prints the §4.2 JSON with `example.com` in `retrieved_urls` | T1 shared trace types, `IsolationError`, the CLI subcommand and its JSON error contract. T2 Codex driver web-agent mode (D-2, D-4, D-4b, D-4c, D-6): feature disables plus the feature-inventory check, positive proof of the offered tool set or an admitted-version record (D-3a), empty working directory, `web` and `none` tool settings, JSONL trace parser written from a committed live capture with requested/final URL, completeness and raw records; hermetic tests (fixture parse, failed fetch not retrieved, non-web or possible-tool unrecognised event → isolation error, non-public fetch target → isolation error, missing or unknown feature → client error, offered tools differ or version not admitted → client error, other unrecognised event → incomplete trace with empty retrieved list, env allowlist); opt-in live tests: G1 and the G3 negative control. If the capture shows no fetch events, stop and surface (D-4) | drivers + cli | security | — |
| 2 | **Claude web agent.** Same command with `--client claude --model claude-haiku-4-5-20251001` prints the same shape | T1 Claude driver web-agent mode (D-3, D-4b, D-4c, D-6): web-only or no tool set with D-3a proof, pre-connection public-target enforcement over resolved addresses and redirect hops that holds under bypass-permissions (fail closed if none passes), settings/MCP isolation, dedicated auth-only configuration home, dedicated multiplexer server, transcript trace parser from a committed live capture; hermetic tests as in slice 1; opt-in live G1 and G3, the latter including the local-file, loopback and resolves-to-loopback fetch attempts. T2 docs: drivers contract (both modes), README usage, AGENTS.md note | drivers + docs | security | 1 |

Parallel eligibility: none (slice 2 reuses slice 1's trace types and CLI).

**Owner prerequisites:** `codex` logged in to the ChatGPT plan; `claude` logged in to Max (into the dedicated configuration home if the client requires it, D-3), plus `tmux`; live tests run with `SUBLLM_LIVE=1`.

## 6. Assumed decisions

- **Python only.** The consumer calls the CLI as a subprocess, so the TS SDK needs no change now.
- **Feature names are verified at run time, not trusted from this doc.** CLIs rename features between versions, and a silently ignored "disable" would reopen the shell.
- **Fixtures come from live captures**, committed with the client version that produced them.
- **The canary file in the negative control lives in a temp directory outside the working directory**, with random contents, so a hit cannot be a coincidence.
- **Bypass-permissions stays on for Claude** in web-agent mode, which is safe only because the offered tool set is proven web-only (D-3a) and the public-target restriction holds under that mode (D-4b). Changing any one requires re-checking the others.

## 7. Deferred hardening

- TS SDK parity for web-agent mode.
- Streaming trace events to the caller during long turns.
- A per-call URL allow/deny list enforced by subllm beyond the public-target restriction (today the consumer decides from the trace after the fact).
- Recording fetch response metadata (status, bytes) when a client exposes it.
- An OS-level filesystem sandbox around the Claude process, as a second boundary behind the proven tool set.

## 8. Review status

spec-review: completed 2026-09-17
