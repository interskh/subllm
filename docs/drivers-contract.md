# Drivers contract

The `subllm/drivers/` package is the only language-specific boundary. A TS/Go
port reimplements just these contracts; the rest of subllm (BaseLLM shape,
fallback, region guard) is trivial to re-express in any language.

Two modes per client: the **text** mode (`codex_exec`, `claude_tmux` — unchanged)
and **web-agent** mode (`codex_web` + `codex_trace`, `claude_web` + `claude_trace`
+ `egress`), specified in
`docs/superpowers/specs/2026-09-17-subllm-web-agent-mode-design.md`. Web-agent
mode is Python-only today; TS parity is deferred (§7 of that spec).

## codex_exec

- Invoke: `codex exec -s read-only --skip-git-repo-check --ignore-user-config
  --ephemeral -C <clean tmpdir>
  -o <tmpfile> [-m MODEL] [-c model_reasoning_effort="EFFORT"]
  [-c web_search="live"] [--output-schema FILE] -- PROMPT`
- NOTE: `--search` is NOT a valid `codex exec` flag (verified on codex-cli
  0.136.0: "unexpected argument '--search'"). Enable web search for exec via the
  config override `-c web_search="live"` instead.
- ISOLATION: `--ignore-user-config` only skips `$CODEX_HOME/config.toml`; codex
  still loads a project `AGENTS.md` from its working directory. Run each call in
  a fresh empty temp dir via `-C <dir>` (also the process `cwd`) so no project
  context leaks in. `--ephemeral` avoids persisting session files.
  The Python driver (`python/src/subllm/drivers/codex_exec.py`) passes
  `--ephemeral` on every call, but passes `-C`/`cwd` and closes stdin only on
  the image path (below). KNOWN GAP: the Python text path still does neither;
  the TS driver does both. Backfill Python to match.
- STDIN: `codex exec` reads stdin even with an argv prompt — the driver must
  close the child's stdin (or it hangs). The TS driver calls `child.stdin.end()`;
  the Python driver should pass an empty stdin.
- Auth: ChatGPT subscription login (no API key). Set `CODEX_HOME` to an isolated
  dir containing only `auth.json` to avoid AGENTS.md context pollution.
  `--ignore-user-config` still reads auth from `CODEX_HOME`.
- Output: the final message is written to `<tmpfile>` (the `-o` path); read it.
- Errors: non-zero exit with stderr+stdout matching
  `/usage limit|rate.?limit|quota|too many requests/i` => QuotaError; missing
  binary / other non-zero exit / timeout => ClientError.
  (NOTE: "try again later" is intentionally excluded — too generic; would
  misclassify auth/network failures as quota.)
- IMAGE COLLECTION (`collect_images`, used by `CodexLLM.generate_image`;
  verified on codex-cli 0.165.0):
  - Every call gets a fresh per-call scratch `CODEX_HOME` holding only
    `auth.json`, even when `codex_home` is given (auth is copied from it).
  - Fresh empty work dir via `-C <dir>`, also the process `cwd`; stdin closed
    (`DEVNULL`). Scratch home and work dir are removed in `finally`.
  - After exit 0 and before cleanup, collect only regular files (no symlinks)
    under `$CODEX_HOME/generated_images/`. Plugin caches hold a `preview.png`
    elsewhere, so the rest of the home is never scanned.
  - The prompt is prefixed with a fixed instruction to call the image tool
    exactly once and not answer with SVG/HTML/code.
  - `references` become `-i <absolute path>` pairs before `--` (absolute because
    the cwd is the throwaway work dir); each must be an existing file, checked
    before the subprocess starts (ClientError).
  - Zero files collected => OutputError. One attempt, no retry.

## claude_tmux

- Build the claude command as ONE shell string (tmux runs it via `sh -c`; split
  trailing argv after the window flags is mis-parsed):
  `claude --session-id <uuid> --permission-mode bypassPermissions [--model M] --tools ''`.
  `--tools ''` disables tools so an unattended turn never deadlocks on approval.
- Spawn: `tmux new-session -d -s <name> -x 200 -y 50 -c <workdir>
  -e CLAUDE_CONFIG_DIR=<cfg> [-e PATH=… -e HOME=… -e USER=… -e LANG=… -e TERM=…]
  '<claude-cmd>'`. The full env-passthrough set is
  `{CLAUDE_CONFIG_DIR, PATH, HOME, USER, LANG, TERM}`.
  Passing `CLAUDE_CONFIG_DIR` via `-e` is REQUIRED — otherwise claude writes its
  transcript somewhere the reader doesn't look and the turn times out.
- Auth: regular Claude subscription (interactive path — NOT `claude -p`, which
  after 2026-06-15 bills a separate programmatic credit).
- Readiness: poll `capture-pane` for the input-prompt markers (`❯`, `│ >`). On
  first run in a project a workspace-trust dialog ("Do you trust the files in
  this folder") appears first — accept it (send `1`, then `Enter`) and keep
  polling.
- Send prompt: for single-line ≤4KB, `send-keys -t <name> -l "<prompt>"` then a
  separate `send-keys -t <name> Enter`. For multiline or >4KB, `load-buffer` +
  `paste-buffer`, wait for the `[Pasted text #N]` placeholder, then `Enter`.
- Detect completion: read the DETERMINISTIC transcript
  `<cfg>/projects/<encoded>/<uuid>.jsonl`, where
  `encoded = re.sub(r"[^A-Za-z0-9-]", "-", str(Path(workdir).resolve()))` and
  `<uuid>` is the `--session-id`. A turn is complete when an
  `{"type":"assistant"}` record has `message.stop_reason == "end_turn"` AND
  contains a text content block. IGNORE partial streaming lines (stop_reason
  null) and thinking-only `end_turn` events. Final text = concatenation of that
  record's text blocks.
- Always `tmux kill-session -t <name>` in a finally block.
- Errors: limit banner in pane (matches any of: "usage limit", "rate limit",
  "out of credit") => QuotaError; tmux spawn failure (new-session non-zero exit)
  / missing tmux binary / timeout => ClientError; no completed text turn =>
  OutputError.

## web-agent mode — shared contract (both clients)

One browsing (or no-tool) turn per call, returning the answer plus a trace of
what the agent searched and fetched. Everything in this section is
client-independent and a port must reproduce it identically for both clients.

- ENTRY POINT: `run_web_task(prompt, schema_model=None, *, tools="web",
  timeout_s=300) -> WebAgentResult` on each client. `tools="none"` is the
  isolated no-tools call for grading untrusted text (D-4c): every isolation
  guarantee, an empty trace, and ANY traced tool event is a violation.
- ONE ATTEMPT, NO RETRY (D-4a). Unlike the text paths, a web-agent call never
  re-runs itself: a silent second browsing session would not be the one the
  consumer timed and grades. The optional region preflight still runs first
  (`RegionError` before any subprocess).
- A model is REQUIRED (`ClientError` when the client has none). Both clients
  bind the trace to the model that actually ran: a rollout/transcript reporting
  a different model than the one requested is a `ClientError`.
- TRACE EVENT: `kind` (`search` | `fetch` | `other`), `raw_type` (the client's
  own event/tool name), `raw` (the client's original record(s), unmodified),
  plus `query` + `result_urls` on a search and `url` (requested) + `final_url`
  (after redirects, when exposed) + `ok` on a fetch.
- RETRIEVED URLS are derived, never taken from the answer: the `final_url` of
  every fetch with `ok is True` and a non-empty `final_url`, deduplicated, in
  first-seen order. Search results are NEVER retrieved URLs.
- URL SPELLING IS THE CLIENT'S AND IS NEVER REWRITTEN (D-4). For the same page
  codex reports `https://example.com/` and claude reports `https://example.com`
  (both measured). G2's uniformity is the trace SHAPE, not the URL text. **A
  consumer that grounds an answer must normalize before comparing.**
- TWO FLAGS FORCE AN EMPTY RETRIEVED LIST BY CONSTRUCTION: `fetch_events_exposed
  = false` (the client does not expose page opens) and `trace_complete = false`
  (an event was unrecognised-but-provably-not-a-tool, unpaired, truncated, or
  there were zero web events on a web-tool call). Both clients currently set
  `fetch_events_exposed = true`, measured. **A run with either flag false is a
  DELIVERY FAILURE for the consumer, not an ungrounded answer** — the retrieved
  list is empty because the evidence is untrustworthy, not because nothing was
  fetched.
- CLASSIFICATION IS AN EXPLICIT ALLOWLIST AND FAILS CLOSED, at every layer
  (record type, item/content-block type, attachment type, tool name). Anything
  unrecognised that *could* be a tool invocation is an isolation violation
  (D-5), not an incomplete trace. `trace_complete = false` is reserved for
  evidence that provably is not a tool call. Consequence a port must accept: a
  benign new record type in a client update fails the run closed.
- ISOLATION ERRORS: `IsolationError` carries the trace and NEVER the answer; the
  answer is not returned beside it. It is NOT a `FallbackLLM` trigger — the other
  client is no safer — and it is never a fallback trigger for the consumer
  either.
- ERRORS (both clients): `QuotaError` on a subscription-limit banner/stderr;
  `ClientError` for missing binary, non-zero exit, timeout, a non-admitted client
  version (D-3a), an unexpected feature inventory, a leaked environment variable,
  a dialog/login prompt, a version or model cross-check mismatch, and an
  unusable `tools` value; `OutputError` only from schema handling (unparseable
  JSON or a schema-validation miss) on a `schema_model` call; `IsolationError`
  for every D-5 / D-4b violation; `RegionError` from the preflight.
  KNOWN DIVERGENCE from the text paths: `claude_tmux` raises `OutputError` when
  no completed text turn exists, while web-agent mode raises `ClientError` for
  the same condition ("no completed text-bearing assistant turn in transcript"),
  and the codex web path does not check the answer file for emptiness at all —
  an empty answer with a complete trace is returned as `answer=""`.
- TIMEOUT: the turn is abandoned and raises `ClientError`; the transcript/rollout
  collected so far is DISCARDED UNJUDGED (both clients). A timed-out call
  therefore yields no trace.
- CLI (§4.2 of the spec):
  `subllm web-agent --client codex|claude --model M [--tools web|none]
  [--schema schema.json] [--timeout S] [PROMPT|-]`.
  Success: exit 0 and ONE JSON document on stdout —
  `{client, model, tools, answer, trace[], retrieved_urls[],
  fetch_events_exposed, trace_complete, client_version, duration_ms}`; each
  trace entry is `{kind, …kind-specific keys…, raw_type, raw}` (search:
  `query`, `result_urls`; fetch: `url`, `final_url`, `ok`; other: neither).
  Failure: non-zero exit and
  `{"error":"quota"|"client"|"output"|"isolation"|"region","message":"…",
  "trace":[…]|null}` on **stdout** — the consumer never parses stderr. `trace`
  is non-null ONLY for `isolation`. **`--schema` is NOT client-side content
  validation:** the CLI adapts the file into a model whose `model_validate`
  accepts any JSON object, so enforcement is the client's own structured-output
  binding — codex binds it natively (`--output-schema`), claude has no such
  binding and is only prompt-instructed, leaving the CLI to check nothing beyond
  "the answer is a JSON object". A caller that needs real validation passes a
  Pydantic model to `run_web_task` from Python. A missing/invalid `--schema` file is
  reported as `"client"` on stdout (the call never reached the model);
  argparse-level errors (unknown flag, bad `--client`) stay argparse's rc 2 on
  stderr. Nothing on this path can leave stdout empty: serialization is total
  (bytes decode, exotic values stringify, lone surrogates stripped, non-finite
  floats become `"nan"`/`"inf"`, `allow_nan=False`), and an ASCII-only stdout
  gets an `ensure_ascii=True` re-dump.
- MAINTENANCE: the D-4b target rule is implemented TWICE — `codex_web`'s
  `_check_fetch_targets` and `claude_web`'s `_check_targets`. They were kept
  separate deliberately (extracting a shared helper would have touched an
  already-gated client path at the end of the run), so **an amendment to one
  must be made to the other**. A parity test runs both over one shared table of
  targets (public, loopback literal, private literal, `file://`,
  resolves-to-loopback, NXDOMAIN, octal spelling, IPv6 loopback) and fails when
  their verdicts diverge — but only for the URLs IN that table: an amendment
  introducing a new class (say, a carve-out for `.internal` names) passes
  silently on both sides. So every amendment must add its own case to the table.
  Do not delete the test without extracting the rule into one shared place first.

## codex_web (web-agent mode, codex-cli)

Measured on codex-cli **0.154.0** with model **gpt-5.6-luna**. Separate entry
point from `codex_exec`; that contract is unchanged.

- INVOKE: `codex exec -s read-only --skip-git-repo-check --ignore-user-config
  --ignore-rules -c project_doc_max_bytes=0 -c web_search="live"
  (--disable FEATURE)*  -C <empty tmpdir> -m MODEL --json -o <tmpfile>
  [--output-schema FILE] -- PROMPT`, with `cwd=<the same empty tmpdir>` and
  stdin at `DEVNULL`.
  With `tools="none"` the one change is `-c web_search="disabled"` (measured:
  merely OMITTING `web_search` still offers a working web tool).
- `--ephemeral` IS DELIBERATELY NOT PASSED, unlike the text mode and unlike a
  literal reading of D-2: it suppresses the session rollout, which IS the trace.
  The rollout is written inside the throwaway `CODEX_HOME` and is deleted with
  it, so nothing persists outside the call.
- `--json` is passed but its stream is NOT the trace: the simplified stream
  collapses a two-URL page open into one item and carries no success signal.
  A port must not build on stdout; only the error blob reads it.
- DISABLED FEATURES (`--disable` each, verified to exist on 0.154.0):
  `shell_tool, unified_exec, unified_exec_tty, browser_use,
  browser_use_external, browser_use_full_cdp_access, computer_use, apps,
  multi_agent, goals, view_image, image_generation, sleep_tool, hooks,
  skill_search, remote_plugin, plugins, in_app_browser, tool_suggest`.
- PREFLIGHT, before the agent runs, in this order:
  1. `codex --version` (client version = the LAST whitespace token of stdout).
  2. ADMISSION (D-3a): this client reports no tool inventory anywhere the driver
     can read (not `session_meta`, `turn_context`, the rollout, or
     `codex debug prompt-input`), so admission is a recorded
     **(version, model)** record — the tool set comes from the per-model
     catalog, so a different model on the same version proves nothing.
     `ADMITTED_VERSION_MODELS = {("0.154.0", "gpt-5.6-luna")}`. Anything else is
     a `ClientError`; there is no escape hatch. **A new pair is admitted only by
     re-running the live G3 negative control and recording the evidence — a
     client auto-update BLOCKS web-agent mode until then, and this will fire.**
  3. FEATURE INVENTORY: `codex features list (--disable FEATURE)*` with the same
     disables. Parse each line as `name … true|false` (last token). Empty
     inventory, a disable whose feature name is missing, a disable that read
     back `true`, or any enabled feature not in `codex_web.ENABLED_ALLOWLIST`
     (30 names, each with its "adds no callable tool" justification next to the
     check) is a `ClientError`. ONE named exception: `unified_exec` reads back
     `true` whatever we pass on 0.154.0 (measured, also with `code_mode_host`
     off), so it is listed in `INEFFECTIVE_DISABLES`. The inventory is a
     narrowing check, NOT proof: it passes while `ALL_TOOLS` still offers
     `apply_patch` and five `multi_agent_v1__*` bindings, which is exactly why
     admission does not rest on it.
- ISOLATION: a throwaway `CODEX_HOME` holding ONLY a copy of `auth.json`
  (copied, not symlinked, from `$CODEX_HOME` or `~/.codex`); a SEPARATE empty
  temp dir as `HOME` (measured: with the real HOME the session's developer
  message listed `/Users/<owner>/.agents/skills`); a third empty temp dir as
  both `-C` and the process cwd. All three are deleted in a `finally`.
  NOTE: `CodexLLM(codex_home=…)` is NOT honoured in web-agent mode — the
  throwaway home is always built; the constructor argument only affects the text
  path. `auth.json` missing at the source is not an error here; the client
  itself fails.
- D-6 ENV ALLOWLIST: the child environment is built from scratch and holds only
  `PATH, HOME, USER, LANG, TERM, CODEX_HOME` (HOME and CODEX_HOME overwritten
  with the throwaway dirs, PATH defaulting to `os.defpath`). Nothing else in the
  caller's environment reaches the child.
- TRACE SOURCE: the session rollout(s), `rollout-*.jsonl` under the throwaway
  `CODEX_HOME`, read before the home is deleted. **More than one rollout is an
  `IsolationError`** — a spawned sub-agent writes its own (measured, with a
  committed parent/child fixture pair). Zero rollouts is a `ClientError`.
- CLASSIFICATION (allowlists; anything else is a violation):
  record types `{session_meta, turn_context, world_state, token_usage_record,
  response_item, event_msg, compacted}`; `response_item` types
  `{message, reasoning, custom_tool_call, custom_tool_call_output}`; `event_msg`
  types `{task_started, task_complete, token_count, item_completed,
  item_started, item_updated, error}` — `turn_diff` is deliberately ABSENT, it
  is positive evidence of a file write; `item_completed` item types
  `{UserMessage, Reasoning, AgentMessage}` plus `Extension` with
  `kind == "web.search"`.
- EVERY TOOL INVOCATION ON THIS CLIENT is a `custom_tool_call` named `exec`
  whose input is JavaScript running in code mode, so D-5 is a SCAN of that
  script. The scan is an allowlist of SHAPE, not a blacklist of spellings (a
  blacklist is bypassable through the prototype chain): allowed globals
  (`tools`, `text`, `JSON`), `tools` usable only as `tools.web__run(`, a
  property denylist (`constructor`, `prototype`, `__proto__`, `call`, `apply`,
  `bind`, `caller`, `arguments`, the `__define*`/`__lookup*` accessors) applied
  in EVERY position, an allowlist of object keys (`open`, `find`, `ref_id`,
  `search_query`, `q`, `response_length`), no computed member access (a numeric
  index is allowed), no `function` keyword (arrow functions are fine), no
  `\u`/`\x` escape sequences anywhere, and each `ref_id`/`find` target must be
  ONE complete string literal (no concatenation, no template literal).
  EXEMPTION: an allowlisted key's own string VALUE is DATA and is exempt from
  the name rules, so ordinary URLs like
  `https://developer.mozilla.org/…/Function/prototype/call` do not trip them; a
  string literal in any OTHER position is still scanned. That exemption says
  "this string is data", never "this target is fine" — the value is still judged
  downstream as a fetch/search target.
  **This is a regex/token scanner, not a JS parser** (limitation, not a claim):
  it is sound for the shapes this client emits and for every bypass found so
  far, and the layers that do not depend on it (the admitted (version, model)
  record, the one-rollout rule, the D-4b target checks, the read-only sandbox,
  the environment allowlist) are what make a scanner miss survivable.
- EVENTS from an `Extension`/`web.search` item, by `action.type`:
  `search` → a search event with `action.query` and the `results[i].url` list;
  `openPage`/`open` and `find_in_page`/`findInPage`/`find` → one FETCH event per
  target. Requested URLs come from the SCRIPT's `open:[{ref_id:…}]` /`find:`
  arrays, paired BY INDEX with `results[i]` — `action.url` is only the first
  ref_id of a multi-open call and must not be used as the fetch list. A
  non-URL `ref_id` (e.g. `turn0search0`) resolves against earlier results'
  URLs; a ref_id that resolves to nothing is a VIOLATION even when the client
  reports a final URL. `ok = bool(results[i].url)`, and that URL is the
  `final_url`. An unknown `action.type` marks the trace incomplete.
  `find_in_page` is ASSUMED to carry `url` in `results[i]` like `openPage` and
  to have no `open:` array — no fixture, capture one live to confirm.
- INCOMPLETE (not violations): `session_meta` with no `cli_version`; a tool
  output with no matching call; a call with no paired output; a stream that
  ended without `task_complete`; zero web items on a `tools="web"` call; no
  `turn_context` model; an open/find whose requested and result counts differ;
  an unknown web action type. A requested `open:` target the client never
  reported back becomes a `fetch` event with `ok=None` (so it still runs
  through the D-4b check) and marks the trace incomplete.
- D-4b (AFTER-THE-FACT AND APPROXIMATE — the honest limitation): this client's
  fetch executes on the VENDOR's servers, so subllm cannot block the connection.
  It can only refuse to return an answer that involved a non-public target, and
  the names are re-resolved later, from this Mac. The fetch's real network
  destinations and intermediate redirect hops are invisible to us. **No reader
  should take the codex path's D-4b check for pre-connection enforcement.** The
  check itself: every fetch's requested AND final URL must be a public
  `http`/`https` target (`publictargets.classify_url`, one cached resolver per
  call); an unresolvable fetch host is a violation (fail closed); alternate IP
  spellings (`0177.0.0.1`, `0x7f000001`, `2130706433`) are refused rather than
  resolved. Search `result_urls` are held to the same check, with ONE carve-out:
  a search host that merely fails to resolve is not a violation (no connection
  was made), while anything non-public by inspection still is.
- JUDGEMENT ORDER after the turn (a port must reproduce it): rollout count →
  parser violations → D-4b targets → rollout `cli_version` vs the admitted
  version → rollout model vs the requested model → build the result.
- ERRORS: as the shared section, plus — non-zero exit matching
  `usage limit|rate.?limit|quota|too many requests` (stderr AND stdout) =>
  QuotaError, any other non-zero exit / missing binary / timeout => ClientError.

## claude_web (web-agent mode, interactive claude over tmux)

Measured on claude **2.1.276** (committed fixtures) and re-verified on
**2.1.277**. Separate entry point from `claude_tmux`; that contract is
unchanged, and none of its mechanics are reused (its shared tmux server would
violate D-6 and its trust-dialog handling answers the wrong option here).

- INVOKE (one shell string, as tmux runs it via `sh -c`, each argv element
  shell-quoted): `claude --session-id <uuid> --permission-mode bypassPermissions
  --model MODEL --tools 'WebSearch,WebFetch' --safe-mode --strict-mcp-config
  --disable-slash-commands`. With `tools="none"`: `--tools ''` (offers no tool
  at all, D-4c).
- FLAGS DELIBERATELY NOT USED: `--restricted` (it REFUSES `bypassPermissions`,
  which D-3 keeps so no prompt can stall an unattended turn) and `--bare` (it
  never reads the OAuth credential, so a Max subscription cannot authenticate).
- ADMISSION (D-3a): `claude --version` (client version = the FIRST whitespace
  token of stdout). This client reports no session-start tool inventory the
  driver can gate on, so a version runs only after passing the live G3 negative
  control: `ADMITTED_VERSIONS = {"2.1.276", "2.1.277"}`, anything else is a
  `ClientError`. **A client auto-update BLOCKS web-agent mode until the control
  is re-run — this already happened once mid-implementation (2.1.276 → 2.1.277),
  and 2.1.277 was admitted by re-running the control, not by widening the set on
  faith.** A positive inventory DOES exist in the transcript (below) but is read
  AFTER the model acted, so it strengthens the gate rather than being it.
- CONFIGURATION HOME (D-3): a throwaway `CLAUDE_CONFIG_DIR` holding exactly two
  seeded files, deleted in a `finally`:
  - `.credentials.json`, mode 0600, containing **only the `claudeAiOauth` key**
    of the owner's keychain blob (`security find-generic-password -s
    "Claude Code-credentials" -w`). The rest of that blob holds `mcpOAuth`
    server secrets, which an auth-only home must not carry. The keychain item is
    namespaced per config home, so a fresh `CLAUDE_CONFIG_DIR` is "Not logged
    in" until this key is copied in.
  - `.claude.json` with `hasCompletedOnboarding`, `theme`,
    `bypassPermissionsModeAccepted`, `firstStartTime`, and a `projects` entry
    keyed by the **resolved** work dir (`/private/tmp/…`, not `/tmp/…`) with
    `hasTrustDialogAccepted`/`hasCompletedProjectOnboarding` and empty
    tool/MCP lists. Without it the theme wizard and the workspace-trust dialog
    block the turn.
  - OPERATIONAL CAVEAT FOR THE OWNER: this COPIES the owner's OAuth credential,
    and the child may REFRESH it as it nears expiry. If the vendor rotates
    refresh tokens, a web-agent call could invalidate the owner's own login. A
    one-time `/login` into a fixed dedicated home would avoid sharing it.
- ISOLATION, the rest: an empty temp dir as `HOME` (measured to work — the
  client authenticates from `CLAUDE_CONFIG_DIR/.credentials.json` alone), a
  separate empty temp dir as the working directory, no settings/MCP/slash
  commands. After the turn, any transcript OTHER than ours in the project dir,
  or any sub-agent directory beside it, is an `IsolationError`.
- D-6 ENV ALLOWLIST: `PATH, HOME, USER, LANG, TERM, CLAUDE_CONFIG_DIR` (HOME and
  CLAUDE_CONFIG_DIR overwritten with the throwaway dirs), plus
  `HTTP_PROXY/HTTPS_PROXY/http_proxy/https_proxy` pointing at the egress filter.
  The caller's `NO_PROXY`/`no_proxy` is NEVER forwarded — it would punch a hole
  in the filter.
- DEDICATED TMUX SERVER (D-6): every tmux call is
  `tmux -L <socket> -f /dev/null …` — a dedicated server, and `-f /dev/null` so
  the owner's `~/.tmux.conf` cannot `set-environment -g` or `run-shell` into it.
  Start it with `new-session -d -s web -x 200 -y 50 -c <work_dir> '<command>'`,
  with the allowlisted env AND `cwd=<work_dir>` (tmux copies its OWN cwd into the
  server's global environment as `PWD`). READ THE SERVER'S ENVIRONMENT BACK
  before killing it: `tmux -L <socket> -f /dev/null show-environment -g`; the
  allowed set is the env allowlist ∪ the proxy vars ∪ `{__CF_USER_TEXT_ENCODING,
  PWD}` (macOS injects the former into every process), any other name is a
  `ClientError`, and `PWD` must BE the empty work dir, not merely be allowed.
  Every successful live run is therefore a D-6 assertion. `kill-server` in a
  `finally`, and a cleanup failure never masks the turn's real result.
- READINESS: poll `capture-pane -p -t web` for up to 60 s. A quota banner =>
  QuotaError. A LOGIN wording (`Not logged in`, `Select login method`,
  `Log in with your Claude account`, `Invalid API key`) => `ClientError` naming
  the cause — **a bare `/login` is deliberately NOT a marker: an AUTHENTICATED
  pane carries "Your login expires in 1 day · run /login to renew" (measured
  live, and matching it failed a working run).** A DIALOG wording (`Quick safety
  check`, `Is this a project you created or one you trust`, `Do you trust the
  files in this folder`) => `ClientError`, never answered: the workspace-trust
  default is "No, exit" and its cursor is one of the input-prompt markers.
  Otherwise wait for the `claude_tmux` banner markers.
- SEND: identical to `claude_tmux` (`send-keys -l` + `Enter` for short
  single-line prompts, `load-buffer`/`paste-buffer` + the `[Pasted text #N]`
  placeholder otherwise), with one addition: a prompt starting with `!` or `/`
  gets ONE leading space, because those characters switch this TUI into bash
  mode / slash-command mode and the prompt is caller-supplied. Precautionary —
  the underlying TUI behaviour is not verified live; a leading space does not
  change what the task asks for, whereas refusing the prompt would fail
  legitimate tasks.
- TRACE SOURCE: the session transcript
  `$CLAUDE_CONFIG_DIR/projects/<encoded resolved cwd>/<session-id>.jsonl`
  (same encoding as the text path). Poll for a completed turn, then wait for the
  file to STOP GROWING (1.5 s quiet, 15 s cap) before reading — the client
  writes trailing tool-result and attachment records after `end_turn`. Read it
  BEFORE `kill-server` (kill adds only `last-prompt` + `cost-state`, measured).
  A tool call is an `assistant` record with a `tool_use` block; its result is the
  `user` record carrying `toolUseResult`.
- CLASSIFICATION (allowlists; anything else is a violation): record types
  `{user, assistant, attachment, system, summary, last-prompt, mode,
  permission-mode, ai-title, atis-latch, queue-operation, cost-state,
  file-history-snapshot, <unparsable>}` — `file-history-delta` is deliberately
  ABSENT and `file-history-snapshot` is allowed only while
  `snapshot.trackedFileBackups` is empty (a non-empty one is positive evidence
  of a file write, mirroring codex's `turn_diff`); `assistant` content blocks
  `{text, thinking, redacted_thinking}` + `tool_use`; `user` content blocks
  `{text, image, tool_result}`; attachment types `{date, date_change, model,
  environment, session_context, total_tokens_reminder, prompt_snapshot,
  instructions, output_style, output_style_instructions, silent_turn_reminder,
  batching_reminder_sent, thinking_stripped, auto_mode, deferred_tools_record,
  deferred_tools_delta, skill_listing, mcp_instructions_delta,
  agent_listing_delta}`. A `tool_use` whose name is outside
  `{WebFetch, WebSearch}` — or ANY `tool_use` under `tools="none"` — is a
  violation, and the violating call's RESULT record is carried in the trace as
  an `other` event (that is where leaked content would appear).
- D-3a STRENGTHENING FROM THE TRANSCRIPT: the `prompt_snapshot` attachment
  carries `tools`, the exact inventory the model was offered
  (`["WebFetch","WebSearch"]` under `tools="web"`, `[]` under `none`, in all
  five captures). A name outside the allowed set is a violation; a transcript
  that reports no inventory at all marks the trace incomplete.
  `deferred_tools_delta`/`deferred_tools_record` naming a non-web tool is a
  violation, and a non-empty `skill_listing`, `mcp_instructions_delta` or
  `agent_listing_delta` means settings/tool isolation failed.
- EVENTS: `WebSearch` → a search event with `input.query` and the URLs under
  `toolUseResult.results[*].content[*].url`. Any other allowed tool name (i.e.
  `WebFetch`) → a fetch event with `input.url` as the requested URL and:
  - `toolUseResult` a plain STRING (the client's own refusal, or the egress
    filter's) → `ok=false`, no final URL — a failed fetch, not unknown evidence;
  - `toolUseResult` a dict → `ok = 200 <= code < 300`; a missing/non-int `code`
    marks the trace incomplete. ONLY on a 2xx is `final_url` set, to the
    client's own `toolUseResult.url` (equal to the requested URL in every
    capture). Grounded in the committed redirect fixture:
    `httpbin.org/redirect-to?url=https://example.com` returns `code: 302` with
    "REDIRECT DETECTED … not fetched automatically" and `url` = the ORIGINAL, so
    a cross-host redirect is never silently followed.
    **Limitation: a SAME-HOST path-level redirect can be followed silently, so
    `final_url` is exact at HOST granularity and may differ in path.**
- INCOMPLETE (not violations): an unparsable transcript line; a tool call with
  no matching result (and an unpaired result, which also becomes an `other`
  event); an unreadable WebSearch/WebFetch result; a WebFetch result with no
  status code; zero web events on a `tools="web"` call; no reported tool
  inventory; no record reporting a version; no assistant record reporting the
  model; no completed assistant text.
- D-4b, PRE-CONNECTION. Measured on 2.1.276/2.1.277: under the confinement
  EVERY connection the client made — `api.anthropic.com`, telemetry, and the
  WebFetch itself — arrived at subllm's filter as a `CONNECT`, so a fetch target
  is judged BEFORE connecting, unlike the codex path. The after-the-fact check
  below still runs on every traced fetch regardless. Two layers:
  1. `drivers/egress.py` — an `EgressFilterProxy` forward proxy on 127.0.0.1.
     It judges every destination with `publictargets.classify_url` BEFORE
     connecting, then connects to the ADDRESS it validated (never re-resolving,
     so a name cannot flip to loopback between check and dial), allows only
     ports 80/443, refuses origin-form requests (`GET /x` + `Host:`) with 400
     rather than routing off a header the client chose, and records every
     `Attempt(at, method, target, host, port, address, allowed, reason,
     connected)`. No TLS interception: a CONNECT is refused or tunnelled
     opaquely; each redirect hop arrives as its own CONNECT and is judged on its
     own. Under the real client every attempt arrives as `CONNECT host:port`
     (it upgrades `http://` to `https://`). A refusal is a 403 and always
     appears in `.attempts`; a 502 means every validated address was
     unreachable, NOT a refusal.
  2. `confined_argv(port, argv)` wraps the command in
     `/usr/bin/sandbox-exec -f <profile> …` (hardcoded path, never
     `shutil.which` — a PATH entry must not be able to satisfy the boundary; a
     missing binary or an unwritable profile is a `ClientError`, never an
     unconfined fallback). The profile is
     `(allow default)(deny network*)(allow network-outbound (remote
     unix-socket))(allow network-bind (local unix-socket))(allow network-bind
     (local ip "localhost:*"))(allow network-outbound (remote ip
     "localhost:<port>"))`.
     **The precise property it gives, stated exactly: the only reachable TCP
     network destination is the filter's port.** Unix-domain sockets and name
     resolution remain available (measured: a confined `curl --unix-socket`
     still reaches a local service), because the client's own IPC and DNS need
     them. **The profile is therefore NOT what confines the model — the proven
     tool set (D-3a) is; the model is offered no tool that can speak to a unix
     socket, and this filter closes the one path it does have. It is not a
     filesystem sandbox** (that is deferred, spec §7).
  Why this is load-bearing: the client self-refuses `file://` ("Invalid URL")
  and bare `localhost` ("cannot fetch localhost … without a dot"), but NOT a
  public-looking name that resolves to loopback — measured, with a
  non-filtering tunnel proxy the real client issued
  `CONNECT 127.0.0.1.nip.io:<port>` and the loopback origin received it.
- **THE EGRESS FILTER IS NOT THE SUBSCRIPTION-TO-API PROXY `AGENTS.md` FORBIDS.**
  It is a network-layer CONNECT filter: it terminates no TLS, reads and rewrites
  no token, rewrites no request shape, and speaks no vendor API. The client
  authenticates to Anthropic itself, natively, over its own TLS. The banned
  thing is a token wrapped behind an OpenAI-compatible endpoint; this only
  decides whether a TCP destination may be reached.
- D-4b, AFTER THE FACT (fires IN ADDITION to the filter):
  - EVERY RECORDED REFUSAL is an `IsolationError`, whether or not a traced fetch
    names its host. The filter's verdict is the authoritative evidence — it is
    where the connection would actually have gone — so skipping a refusal
    because the host is also traced, and letting the later check re-resolve it,
    would return the answer whenever the name moved back to a public address
    (DNS rebinding, a TTL rolling to a CDN). ONE carve-out: a refusal whose
    reason matches `^port \d+ is not one of ` is POLICY, not isolation, because
    `judge_target` checks publicness BEFORE the port rule — that reason proves
    the target WAS a public http/https host. That ORDERING is load-bearing;
    swapping it would re-open the rebinding hole through this door.
  - Then the same target check as the codex path: every fetch's requested and
    final URL must be public (unresolvable = violation), and search
    `result_urls` with the unresolvable carve-out.
  - Each fetch event's `raw["egress_attempts"]` carries the filter's attempts
    for that host, so a refusal — where the client reports only an error string
    — is diagnosable from the result itself.
- JUDGEMENT ORDER after the turn (a port must reproduce it): tmux server
  environment → sibling/sub-agent transcripts → parser violations → recorded
  refusals → D-4b targets → transcript version vs the admitted version →
  transcript model vs the requested model → answer present → build the result.
- OPERATIONAL NOTES: the isolated config home auto-downloads the official plugin
  marketplace from `raw.githubusercontent.com` on every call (it adds no tool —
  the per-run inventory proves it); all client-internal destinations
  (`api.anthropic.com`, `raw.githubusercontent.com`) were allowed in every
  capture, so a client update adding an endpoint on a non-80/443 port would trip
  the refusal rule and fail every run closed — read `egress_attempts` in the
  error's trace first. The `session_context` attachment injects the OAuth
  account's email into the model's context: auth-derived, not
  owner-config-derived, but it is the one piece of owner identity in the
  session.


## chatgpt_web (ChatGPT web route)

Spec: `docs/superpowers/specs/2026-10-31-subllm-chatgpt-image-and-web-design.md`
(D-3 to D-7, section 4.3). Python only. The route drives the official chatgpt.com
web app in a real logged-in browser; it never calls `backend-api` directly.

- PAGE CORE (D-3). All chatgpt.com knowledge lives in `page_core.js`, pure
  functions run in the page through `evaluate`: `state()` (logged_in, login_prompt,
  challenge, mode_surface, layout, busy, quota), `composerMatches(prompt)`,
  `acceptance()` (pending | accepted | rejected, turn_key, conversation_url),
  `turnStatus(turn_key)`, `captureText(turn_key)` (Copy-button markdown, falls
  back to `innerText`), `servedModel(turn_key)`, `servedModelMenu(turn_key)`, `selectMode(target)`, `auditModel(mode, served)`.
  The core never generates input. Error-text scans (`acceptance` rejection, `turnStatus.error_dialog`)
  skip the user message and the assistant answer body, so a prompt or answer
  that starts with "Unknown error" is not read as a failure.
- INPUT (D-3a). Bring the tab to front (`Page.bringToFront`), type with per-key
  events and a 40 ms delay, send with Enter. Never `insertText` or paste: a
  filled-without-keys composer was rejected by the server (403 on `prepare`).
  Accepted means the URL changed from no conversation id to `/c/<uuid>`. "Unknown
  error" while the URL is still a `local-chatgpt:` placeholder is a rejection.
- PROMPT NORMALIZATION. Before generating the script: CRLF and CR become LF, tab
  becomes 4 spaces (`keyboard.type` presses Enter for CR and moves focus for tab).
  LF is typed as Shift+Enter.
- SCRIPT (D-4). One Node script per call: a preamble (`ARGS`, `KEYPRESS_FILE`,
  `CORE` as JSON literals) plus the `ego_run.js` body, piped to `ego-browser
  nodejs` on stdin. The runtime inherits no cwd or env. The task space is fresh
  and named `subllm-chatgpt-<8 hex>`. The script calls `finish({keep: []})` on
  every outcome except `human_action`, where it calls `handOff()` and leaves the
  space open.
- DETECTION (D-4, D-5). `ego-browser` on PATH, `ego-browser --version` exits 0, a
  no-browser health script round-trips a sentinel. Then `ego-browser skill` must
  report usage guide major `2`; any other major is a `ClientError` naming both
  versions, never a fallback, even under `browser="auto"`. Under `auto`, a failed
  ego ladder (before the guide check) falls back to the Chrome driver; if Chrome
  is also unavailable the `ClientError` names both reasons. `browser="ego"` and
  `browser="chrome"` never fall back. The choice is made once at resolution,
  before any send; an error from the resolved driver is never retried on the
  other one.
- CHROME SUBSTRATE (D-5, optional extra `subllm[chrome]`). patchright persistent
  context, channel `chrome`, headful, on the owned profile
  `~/.local/share/subllm/chrome-profile`. Page-core reads run in the isolated
  world (stealth); only `captureText` (the page's Copy handler) and the image
  `fetch` run in the main world. Image bytes come back as base64 from an in-page
  `fetch` bounded at 30 s, are written to the per-call temp dir, and then go
  through the same IMAGE SAVE path. The keypress record is in-process (`sent` is
  set before the first key event). The same `classify` mapping and the same
  `flock` as ego apply. On `human_action` the context is closed and a detached
  login helper opens a Chrome window on the profile; the call raises
  `HumanActionRequired(browser="chrome")` and the helper takes no lock. The guide
  check is a no-op. Chrome is found only at patchright's channel paths
  (`/Applications/Google Chrome.app`, `/opt/google/chrome/chrome`, Windows
  `Program Files`/`LOCALAPPDATA`). OVERALL LIMIT: a timer armed after launch uses ego's
  budget formula; on expiry it SIGKILLs the browser pid read from the profile's
  `SingletonLock` symlink (never by name), the blocked `evaluate` raises, and the
  outcome goes through `classify` with `expired` (ClientError before the send,
  SendUnconfirmedError after). Verified against offline fixtures only, not live.
- ONE SEND PER CALL (D-6). Every call starts in a new chat (confirm no `/c/` in
  the URL before typing). No retry anywhere. Phases split at the first typed key
  (the keypress record, below) and at server acceptance: before it, failures are `ClientError`,
  `QuotaError` or `HumanActionRequired`; after it, only a positive rejection
  is `ClientError`; everything else that does not end in a result is
  `SendUnconfirmedError` (with the conversation URL when accepted). Calls are
  serialized per machine by an exclusive `flock` on
  `~/.local/share/subllm/chatgpt-web.lock`; waiting longer than `timeout_s`
  is a `ClientError`.
- ERRORS (D-7).

  | Condition | Error |
  |---|---|
  | usage-limit dialog before the key | `QuotaError` |
  | send rejected before acceptance | `ClientError`, no resend |
  | any other failure after the key | `SendUnconfirmedError` |
  | logged out, challenge | `HumanActionRequired` (CLI exit 3) |
  | Work mode, mode unconfirmed, browser missing, script crash before send, guide mismatch | `ClientError` |
  | completed turn with empty text | `OutputError` |

- RESULT PROTOCOL (4.3). The script prints tagged lines; the parser reads stdout
  and stderr.

  ```
  SUBLLM_EVENT {"phase": "keypress"}
  SUBLLM_EVENT {"phase": "accepted", "conversation_url": "https://chatgpt.com/c/<id>"}
  SUBLLM_EVENT {"phase": "rejected"}
  SUBLLM_RESULT {"ok": true, "text": "...", "format": "markdown"|"plain", "images": [], "served_model": "..."|null, "conversation_url": "..."}
  SUBLLM_RESULT {"ok": false, "kind": "human_action"|"quota"|"client"|"output"|"downgrade", "detail": "...", "conversation_url": ..., "text": ..., "served_model": ...}
  ```

  `rejected` is emitted when `acceptance()` reports a rejection, and Python maps a
  `client` failure with that event to "send rejected; not resent". Exactly one
  `SUBLLM_RESULT` per run; the exit status is authoritative, so an `ok: true` line
  with a non-zero exit is not trusted.
- DURABLE KEYPRESS RECORD. Right before the FIRST typed key (any key event can
  send early, e.g. a lost Shift on Shift+Enter) the script writes `KEYPRESS_FILE`
  synchronously, once per run, and emits the keypress event. The
  parent reads that file, not the stream, to decide whether a send may have
  happened. File present and no usable result is `SendUnconfirmedError`; file
  absent and no result is `ClientError` with the stderr tail.
- EARLY SEND. Because the record already exists, every failure after typing starts
  (a throwing typing or URL call, a composer mismatch, a `/c/` URL before Enter)
  is a `SendUnconfirmedError`, never a plain `ClientError` for a possibly sent
  prompt. The `/c/` check still runs BEFORE the composer check so the detail
  reads "chat opened before the send key".
- NOT-READY PAGE. The script waits up to 45 s for `logged_in` or `challenge`; a
  login control alone does not end the wait (it can show before the session
  loads). A page that never shows the composer is a `human_action` only
  when `state()` reports `challenge` or `login_prompt`; otherwise it is a
  `ClientError` ("chatgpt.com did not become ready") and no hand-off happens.
- EMPTY PROMPT. A prompt that is empty after normalization is a `ClientError`
  before any subprocess or lock.
- IMAGE MODE. `ARGS.kind == "image"`; the prompt is prefixed "Generate an image: ".
  The wait loop ends when no Stop button is rendered and the set of loaded images
  (wider than 128 px, in the new turn, outside user units) is non-empty and
  unchanged between two reads. A finished turn with no images for 8 s is
  `output` ("no image in the answer"), so a text-only answer is `OutputError`.
  The Copy button is not required.
- IMAGE SAVE. For each image the script calls `page.fetch(src, {saveAs})` into
  `img-<n>` inside the per-call temp dir (`ARGS.image_dir`); a failed fetch is a
  `client` failure. The ok line lists the paths in `images`. Python accepts a
  path only if it is a regular, non-symlink file directly inside that temp dir,
  detects the format from magic bytes (PNG, WebP or JPEG; never from the name),
  validates every file before copying any, then copies each into `out_dir` with
  exclusive create. A missing, empty-list, outside or non-image file, or a failed copy into `out_dir`, after an ok
  result is a `SendUnconfirmedError` with the conversation URL.
- IMAGE BUDGET. The subprocess budget is `timeout_s + 120 + 0.06 * len(prompt)`
  seconds, plus 60 s for image runs. Image runs are never retried.
- MODE SELECTION (D-8). `selectMode(target)` is a pure step planner for the composer's
  "Thinking effort" slider (levels Instant, Medium, High, Extra High, Pro; `thinking`
  = Medium, `pro` = Pro, or a level name). Radix opens on real pointer input, so the
  core never dispatches events: each call returns `{action, current, ...}` with
  `action` one of `click` (`selector`), `focus` (`selector`), `key` (`key`), `done`
  (`selected_label`) or `fail` (`detail`), and the substrate performs it with real
  input and asks again. `current` is the level read from the trigger label (or from
  the open menu's status line). Only `done`, meaning the closed trigger's label equals
  the target, counts as selected. The loop is bounded at 16 steps with 350 ms between
  them; running out of steps is a failure. A missing picker (legacy layout), an
  unknown level or a locked slider is `fail`.
- MODE ORDER. The picker runs after the pre-send checks and the new-chat check and
  before the composer is focused and the keypress record is written. After a
  selection, `state()` is read again and must be logged in, in Chat mode and not
  busy (a usage-limit dialog is `quota`); otherwise `client` ("Chat mode not
  confirmed after mode selection"). A failed or unconfirmed selection is `client`
  with nothing typed. `mode="default"` skips the picker and the audit entirely.
- SERVED MODEL. The completed turn's DOM carries no model label on the current
  layout. The label is in the menu of the turn's `button[aria-label='Regenerate
  response']` (`aria-haspopup="menu"`, a Radix `id`): the first menuitem reads
  "Try again • <model>". For a non-default mode, after capture and before the audit,
  the script calls `servedModelMenu(turn_key)` (`{selector}` of that button, or null
  unless the button is rendered, declares `aria-haspopup="menu"` and has an id), does
  a real click on the selector, waits about 600 ms, reads `servedModel(turn_key)`
  (the text after "•" in the first "Try again" item of the open `[role=menu]` whose
  `aria-labelledby` is that button's id; else `data-message-model-slug`; else null),
  and always presses Escape (about 300 ms) afterwards. It never clicks a menuitem or
  presses Enter, because that would regenerate the answer. Errors in this step are
  swallowed and leave `served` null (unverified). `mode="default"` makes no menu
  interaction and reads `servedModel` as before, so its `served_model` is null.
- AUDIT. After the answer is captured and the served model is read, for a non-default
  mode the script calls `auditModel(mode, served)`: `unverified` (no label), `ok` (mode is
  not `pro`, or the label contains the word "pro"), or `downgrade`. `downgrade` ends
  the run (before any image is fetched) with `SUBLLM_RESULT {"ok": false, "kind":
  "downgrade", "text": ..., "served_model": ..., "requested_mode": ...}`, which
  Python raises as `ModelDowngradeError`. `unverified` returns the result with
  `served_model: null`.
- MODE RESTORE. The first `current` seen by the picker is the restore target; it is
  set when it differs from the selected label or the selection failed (so an opened
  menu is closed again). The target is recorded from the first planner step, before
  any action, so a throw mid-loop still restores. After the outcome, the same bounded
  loop selects it back. Restore is best effort: errors are swallowed, it never changes
  the result, and it is skipped for `human_action` (ego) and when the Chrome call was
  killed by its limit before the result was decided. The ego restore is bounded to
  about 15 s (`picker restore timed out` is logged and the script finishes). A Chrome
  watchdog expiry that fires during the restore, after the flow's result is decided,
  may still kill the browser but is not used to classify that result. The choice persists in the browser/account, which is why it is restored.
- TIMEOUT. `timeout_s` defaults to 2700 s for `mode="pro"` and 600 s otherwise (the
  flock wait uses the same value).
