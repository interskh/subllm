# subllm

**Run LLM requests through your flat-rate coding subscriptions (Codex/ChatGPT
plan, Claude Pro/Max) via the genuine official clients — no per-token API
billing, no request-rewriting proxy.**

`subllm` drives the real official CLIs as subprocesses (`codex exec`, and
interactive `claude` over tmux) behind a small `BaseLLM` seam, so any project
that already codes against `complete` / `complete_json` / `complete_json_schema`
can swap a per-token client (e.g. `GeminiLLM`) for a subscription-native one with
no call-site changes.

**Status:** v1 implemented (CodexLLM + ClaudeLLM + FallbackLLM + CLI), plus an
opt-in ChatGPT web route for text and images (`ChatGPTWebLLM`). 665 hermetic tests passing,
plus an opt-in live suite — the codex and claude paths verified end-to-end against
live subscriptions.

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
- **Web-agent mode** — one isolated browsing turn per call on either client,
  returning the answer *plus a trace* of what was searched and which pages were
  actually retrieved, so a consumer can ground an answer instead of trusting it.
  The agent is offered web search and web fetch and nothing else; any other tool
  event fails the call loudly.

## Layout

```
subllm/
  python/                # Python library (CodexLLM + ClaudeLLM + FallbackLLM + CLI)
    src/subllm/
      __init__.py          # public exports (the 14 names below)
      base.py              # BaseLLM ABC + transient _retry + DryRunLLM
      errors.py            # SubllmError / QuotaError / ClientError / OutputError / RegionError / IsolationError
      preflight.py         # RegionGuard (optional region/IP check)
      codex.py             # CodexLLM  (primary)
      claude.py            # ClaudeLLM (fallback)
      fallback.py          # FallbackLLM(primary, *fallbacks, on=(QuotaError,))
      webagent.py          # TraceEvent / WebAgentResult (the web-agent trace contract)
      publictargets.py     # "is this a public web target?" (resolution-aware)
      cli.py               # minimal CLI: subllm complete / complete-json / web-agent
      drivers/
        codex_exec.py      # contract for `codex exec`
        claude_tmux.py     # tmux driver for interactive `claude`
        codex_web.py       # web-agent mode for codex (isolation + admission)
        codex_trace.py     # codex rollout -> trace, isolation checks
        claude_web.py      # web-agent mode for claude (isolation + admission)
        claude_trace.py    # claude transcript -> trace, isolation checks
        egress.py          # pre-connection egress filter + sandbox-exec confinement
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
- For **web-agent mode**, additionally:
  - `codex`: an admitted **(client version, model)** pair — currently only
    codex-cli `0.154.0` with `gpt-5.6-luna`.
  - `claude`: an admitted **client version** (currently `2.1.276` / `2.1.277`),
    `tmux`, a `claude` logged in **from the default config home** (the
    credential is read from the keychain item `Claude Code-credentials`; a login
    made under a custom `CLAUDE_CONFIG_DIR` is stored under a different,
    namespaced item and will not be found), and **macOS `sandbox-exec`** at `/usr/bin/sandbox-exec` — the
    call fails closed without it, never running unconfined.
  - `SUBLLM_LIVE=1` to run the opt-in live web-agent suite.

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

### Image generation (Codex route)

```python
from subllm import CodexLLM

res = CodexLLM().generate_image("A red circle on white.", "out/")
print(res.paths)   # [Path("/abs/path/out/20261031T120000-1a2b3c4d-1.png")]  (absolute)
```

Spends Codex quota (about 46 s per image). Files are saved as
`<UTC ts>-<8 hex>-<n>.png` and never overwrite existing files. It makes one
attempt with no retry, so a refusal or timeout does not re-spend quota. It
raises `OutputError` when codex produces no image, and `ClientError` when
`out_dir` is not a writable directory (checked before codex starts).
`references=[path, ...]` attaches image files to the prompt (codex `-i`), for
example a character sheet the new image must match; a missing file raises
`ClientError` before codex starts. `CodexLLM` implements the `ImageGenerator`
interface.

### Choosing a route

| | Codex route | Web route |
|---|---|---|
| Client | genuine `codex exec` | chatgpt.com in a real logged-in browser |
| Quota | Codex/Work | Chat only |
| Images | `image_gen`, about 1536x1024 | the page's rendered size (1254x1254 observed), slower |
| Modes | none | `thinking`, `pro` (needs `plan="pro"`) |
| Concurrency | none enforced | one call at a time per machine (file lock) |
| History | no chat history | regular chats: history, memory and custom instructions apply |
| Account risk | normal Codex use | see below |

The routes never cross: neither falls back to the other. `FallbackLLM` is the
explicit composition, and only for text; it does not dispatch `generate_image`.

Account risk (spec A-1): driving the chatgpt.com web UI programmatically is against
the ChatGPT Terms clause on programmatic extraction of output. There are no recorded
bans for paid accounts driven in a real browser, but absence of reports is not
safety. The web route is opt-in only: never the default and never in a default
fallback chain. Keep to personal volume, concurrency one and a residential network.
subllm never solves a CAPTCHA: a challenge raises `HumanActionRequired` (CLI exit 3)
and hands the page to you.

With `mode="pro"` the Pro usage cap may downgrade the model silently. The
guarantee is the pre-send check of the picker (below); the post-run audit only
catches a model label the page shows in the answer's "Try again" menu (below).

### ChatGPT web route (ego lite or Chrome)

`ChatGPTWebLLM` sends a prompt through the official chatgpt.com web app, open in
a real, logged-in browser: ego lite, or Google Chrome as a fallback. It spends **Chat** quota only. It is opt-in:
nothing else in subllm uses it, and `FallbackLLM` only reaches it if you list it.
Browser automation of the web app carries account risk (spec A-1); use an
account you can afford to put at risk.

Each call opens a regular chat that stays in your ChatGPT history. Your account's
memory and custom instructions can colour answers (the web analogue of Codex's
AGENTS.md pollution).

Prerequisites: ego lite installed and running, and one manual login to
chatgpt.com in its profile. If the page shows a login wall or a challenge,
`HumanActionRequired` is raised and the space is handed to you.

```python
from subllm import ChatGPTWebLLM

llm = ChatGPTWebLLM()                       # browser="auto", plan="plus"
text = llm.complete("Summarize ...")
res = llm.complete_result("Summarize ...")  # WebTextResult
print(res.format, res.served_model, res.conversation_url)  # https://chatgpt.com/c/<id>
data = llm.complete_json("Return {...}")    # dict, prompt-instructed JSON
```

A call makes exactly one send and never retries; calls are serialized per machine
by a file lock (shared by both browsers).

#### Modes and plan

```python
llm = ChatGPTWebLLM(mode="pro", plan="pro")   # mode: default | thinking | pro
```

- `mode` picks the reasoning level in the composer's "Thinking effort" slider
  (Instant, Medium, High, Extra High, Pro): `thinking` selects Medium, `pro` selects
  Pro, `default` leaves the picker alone.
- `plan` (`free` | `plus` | `pro`, default `plus`) gates modes at construction:
  `mode="pro"` with any other plan raises `ValueError`. A bad `mode`, `plan` or
  `browser` value also raises `ValueError`.
- The selection is read back from the picker label before the send. If it does not
  match, the call fails closed with `ClientError` and nothing is sent. The legacy
  layout has no picker support, so a non-default mode fails closed there.
- The choice persists in the browser and account, so the driver restores the
  previous level afterwards (best effort; it never changes the call's result). The
  restore is bounded to about 15 s in the ego driver, and a timeout that fires
  during the restore in the Chrome driver does not affect the result either.
- `timeout_s` defaults to 2700 s for `pro` and 600 s otherwise; an explicit value
  wins. The wait for the machine-wide lock uses the same value.
- `ModelDowngradeError` is raised when a Pro run's page names a model that is not
  Pro. It carries `.text` (the answer), `.served_model` and `.requested_mode`. A page
  with no model label gives `served_model=None`, which means unverified, not
  passed. The current layout shows the served model only in the answer's "Try again"
  menu ("Try again • <model>", for example "6 Pro" on the live Pro check). For
  `thinking` and `pro` the driver opens that menu with a real click (only when the
  button declares a menu), reads it, and closes it with Escape without choosing an
  item. Default mode does not, so its `served_model` is `None`.
- A Pro image call whose served model is not a Pro model raises
  `ModelDowngradeError` before the images are saved (the audit runs before the
  fetch), so no image files are written.

#### Choosing the browser

- `browser="auto"` (default): ego lite when its detection passes, else Chrome. If
  neither is available, `ClientError` names both reasons.
- `browser="ego"` / `browser="chrome"`: force one browser; there is no fallback.
- The choice is made once, before anything is typed. An ego guide-major mismatch
  is an error, never a fallback. After the first key, or after any error from the
  chosen browser, no second browser is tried.

#### Chrome driver

Install the extra and Google Chrome:

```bash
pip install 'subllm[chrome]'     # or, in a checkout: uv sync --extra chrome
```

Google Chrome must be installed where patchright's `chrome` channel looks: macOS
`/Applications/Google Chrome.app`, Linux `/opt/google/chrome/chrome`, Windows under
`Program Files` or `%LOCALAPPDATA%` (a copy in `~/Applications` or another PATH
location is not found). The driver uses patchright with channel
`chrome`, so no Chromium is downloaded. It runs headful on a subllm-owned
profile at `~/.local/share/subllm/chrome-profile`, not your daily profile.

One-time login: on first use, or when chatgpt.com shows a login wall, a Chrome
window opens on that profile and `HumanActionRequired` is raised (CLI exit 3).
Log in, close the window, and retry. Calls fail while that window is open.

A call is bounded: if Chrome stops responding for `timeout_s` + 120 s (+ prompt-length
and image allowances, the same budget as ego lite), the driver kills the Chrome
that owns the subllm profile and raises (`ClientError` before the send,
`SendUnconfirmedError` after it).

The Chrome path to chatgpt.com has **not been verified live**: it is covered by
offline fixture tests only (`SUBLLM_BROWSER=1`, see Testing).

#### Image generation (web route)

```python
res = ChatGPTWebLLM().generate_image("A red circle on white.", "out/")
print(res.paths, res.conversation_url)   # route == "web"
```

Spends Chat quota (about 40 s per image). The prompt is prefixed with
"Generate an image: " and every call opens a new chat. Files are named like the
Codex route (`<UTC ts>-<8 hex>-<n>.<ext>`) and are PNG, WebP or JPEG at the size
the page renders (1254x1254 observed). A text-only answer raises `OutputError`.
Once the prompt may have been sent, any failure to get the files (a missing or
non-image file, a failed write into `out_dir`, a timeout) raises `SendUnconfirmedError` with the conversation
URL; the call is never retried, so quota is not spent twice.

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

### Web-agent mode (browse the public web, with a trace)

One isolated browsing turn per call, on either client. The agent is offered web
search and web fetch only; it gets a throwaway client configuration home, an
empty `HOME` and working directory, and an environment allowlist, so nothing of
your setup or your secrets enters the session.

```python
from subllm import CodexLLM, ClaudeLLM, IsolationError

llm = CodexLLM(model="gpt-5.6-luna")                      # or
llm = ClaudeLLM(model="claude-haiku-4-5-20251001")

result = llm.run_web_task(
    "Open https://example.com and give its title.",
    tools="web",        # or "none": same isolation, no tools at all (for grading
                        # untrusted text); any tool event is then a violation
    timeout_s=300,
)
print(result.answer, result.retrieved_urls)
```

`run_web_task` **ignores the `codex_home` constructor argument**: it always
builds a throwaway home, copying `auth.json` from `$CODEX_HOME` (or `~/.codex`)
into it. For claude it reads the OAuth credential from the macOS keychain.

`run_web_task(prompt, schema_model=None, *, tools="web", timeout_s=300)` also
takes a Pydantic model, exactly like `complete_json_schema`, and then returns a
validated `dict` as `answer`.

`WebAgentResult` fields, and what each means when you ground an answer:

| Field | Meaning |
|---|---|
| `answer` | `str`, or `dict` when a `schema_model` was given |
| `trace` | ordered `TraceEvent`s: `kind` is `search` / `fetch` / `other`, with `query` + `result_urls` on a search, `url` (requested) + `final_url` (after redirects) + `ok` on a fetch, and the client's untouched record in `raw` |
| `retrieved_urls` | the `final_url` of every *successful* fetch, deduplicated, first-seen order. **Ground against this list only** — search results are not retrieved pages, and URLs the answer cites are not evidence |
| `fetch_events_exposed` | `False` → `retrieved_urls` is empty *by construction* |
| `trace_complete` | `False` → `retrieved_urls` is empty *by construction*: some evidence was unpaired, truncated or unrecognised |
| `tools` | `"web"` or `"none"`, as requested |
| `client`, `model`, `client_version`, `duration_ms` | what produced the trace |

Three things a consumer must handle:

- **URLs are the client's own spelling and are never rewritten.** For the same
  page codex reports `https://example.com/` and claude reports
  `https://example.com`. The trace *shape* is uniform across clients; the URL
  text is not. **Normalize before comparing.**
- **`trace_complete=False` or `fetch_events_exposed=False` is a delivery
  failure, not an ungrounded answer.** The retrieved list is empty because the
  evidence is untrustworthy — re-run rather than scoring it.
- **`IsolationError` is a hard stop.** It carries `.trace` and never the answer,
  it is never a `FallbackLLM` trigger, and it should not be one for you either.

Web-agent calls make **one attempt and never retry** (unlike the text methods): a
silent re-run would return a different browsing session than the one you timed.

Known limits, stated as limits:

- **codex:** the fetch runs on OpenAI's servers, so the public-target check is
  *after the fact and approximate* — subllm cannot block that connection, only
  refuse to return an answer that involved a non-public target. Its isolation
  scan of the client's exec scripts is a token scanner, **not a JS parser**.
- **claude:** every connection the confined client made arrived at subllm's
  filter as a `CONNECT` (measured on 2.1.276/2.1.277), so a fetch target is
  judged *before* connecting; the after-the-fact target check still runs on
  every traced fetch regardless. `final_url` is exact at
  **host** granularity — a same-host path redirect can be followed silently. The
  sandbox profile bounds **TCP destinations only** (unix sockets and DNS remain);
  it is not a filesystem sandbox.
- **Both:** a timeout discards the transcript unjudged, so a timed-out call
  yields no trace.
- **claude, operational:** the call copies your OAuth credential into the
  throwaway config home and the child may refresh it, so if the vendor rotates
  refresh tokens this could affect your own login. A one-time `/login` into a
  fixed dedicated home would avoid sharing it.
- **Both:** a client auto-update **blocks web-agent mode** until the live
  negative control is re-run and the new version is admitted. This is by design
  (a new version's tool set is unproven) and it has already fired once.

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
subllm complete       --client chatgpt-web [--browser auto|ego|chrome]
                      [--mode default|thinking|pro] [--plan free|plus|pro] [PROMPT]
subllm web-agent      --client codex|claude  --model M [--tools web|none]
                      [--schema schema.json] [--timeout S] [PROMPT]
subllm image          --route codex|web -o OUT_DIR [--ref FILE ...] [--browser auto|ego|chrome]
                      [--mode default|thinking|pro] [--plan free|plus|pro] [PROMPT]
```

`PROMPT` comes from the argument or stdin (`-`). For `complete` /
`complete-json` the result goes to stdout and errors to stderr with a non-zero
exit code. Example:

```bash
CODEX_HOME=/tmp/codex-clean subllm complete --client codex "Say hello in five words."
```

`--browser chrome` needs `subllm[chrome]` and Google Chrome (see "Chrome
driver"); a Chrome login request exits 3 like ego. With `--client chatgpt-web`, `--effort` and `--model` are rejected (exit 2), and
`--browser`, `--mode` and `--plan` are rejected for the other clients. Errors print
`error: <Type>: <message>` to stderr and exit 1; `HumanActionRequired` exits 3
so a wrapper can prompt the user to log in.

`--mode` and `--plan` work on `complete`, `complete-json` and `image` for the web
route only; with `--client codex|claude` or `--route codex` they are rejected
(exit 2). `--plan` falls back to `$SUBLLM_CHATGPT_PLAN` (empty counts as unset),
then `plus`. An invalid `$SUBLLM_CHATGPT_PLAN` is a usage error (exit 2) only when
`--plan` is absent and the web route is used. `--mode pro` without plan `pro` is
also a usage error (exit 2). A `ModelDowngradeError` prints
`error: ModelDowngradeError: ...` and exits 1.

`image` prints one JSON line (`paths`, `route`, `text`, `conversation_url`,
`served_model`) to stdout. On failure it prints `error: <Type>: <message>` to
stderr and exits 1. `--ref FILE` (repeatable) attaches reference images on the
codex route and is rejected for `--route web` (exit 2). With `--route web`,
`--browser` selects the browser (rejected for `--route codex`, exit 2), a `SendUnconfirmedError` line ends with
`(conversation: <url>)`, and `HumanActionRequired` exits 3.

### `web-agent` (machine-facing)

`web-agent` is the entry point for non-Python consumers: **one JSON document on
stdout, success or failure** — never parse stderr.

```bash
subllm web-agent --client codex  --model gpt-5.6-luna \
  "Open https://example.com and give its title."
subllm web-agent --client claude --model claude-haiku-4-5-20251001 "…"
```

Success, exit 0:

```json
{"client":"codex","model":"…","tools":"web","answer":"…",
 "trace":[{"kind":"search","query":"…","result_urls":[],"raw_type":"…","raw":{}},
          {"kind":"fetch","url":"https://…","final_url":"https://…","ok":true,
           "raw_type":"…","raw":{}}],
 "retrieved_urls":["https://…"],"fetch_events_exposed":true,"trace_complete":true,
 "client_version":"…","duration_ms":12345}
```

Failure, non-zero exit, still on stdout:

```json
{"error":"quota"|"client"|"output"|"isolation"|"region","message":"…",
 "trace":[…] | null}
```

`trace` is non-null **only** for `"isolation"`.

> **`--schema` on the CLI is not content validation.** The file is bound
> natively by codex (`--output-schema`); claude has no such binding and is only
> prompt-instructed, and in both cases subllm itself checks only that the answer
> is a JSON object. For enforced validation, call `run_web_task` from Python
> with a real Pydantic model.

A bad `--schema` file is
reported as `"client"` on stdout; argparse-level errors (unknown flag, bad
`--client`) stay argparse's own exit 2 on stderr.

## Testing

The default suite is **hermetic** — it stubs `codex`/`tmux` with fake executables
on `PATH`, so it never touches a real subscription or the network:

```bash
cd python
uv run pytest                 # no subscription, no network (665 passed, 119 skipped)
```

The web-agent parsers are tested the same way: their fixtures are **sanitized
recordings of real runs** (`tests/fixtures/codex-0.154.0-*.jsonl`,
`claude-2.1.276-*.jsonl`), committed with the client version that produced them.

A separate **opt-in live suite** drives the real subscriptions to confirm both
clients work end-to-end. It is skipped unless `SUBLLM_LIVE=1` is set (so CI and a
normal `pytest` run never bill you), and each client self-skips if its binaries
aren't on `PATH`; a `QuotaError` becomes a skip rather than a failure:

```bash
SUBLLM_LIVE=1 uv run pytest -m integration            # codex + claude
SUBLLM_LIVE=1 uv run pytest -m integration -k codex   # codex only
SUBLLM_LIVE=1 uv run pytest -m integration -k claude  # claude only
SUBLLM_LIVE=1 uv run pytest -m integration -k image   # codex image (spends quota); the web image test also needs SUBLLM_WEB=1
SUBLLM_LIVE=1 SUBLLM_WEB=1 uv run pytest -m integration -k chatgpt_web  # ego lite + chatgpt.com login
```

Requires `codex` logged in to a ChatGPT plan; the claude tests additionally need
`claude` (logged in to Pro/Max) and `tmux`.

Opt-in **browser fixture tests** load DOM fixtures into a real ego lite page and
headless Chrome with the network blocked (they never reach chatgpt.com). Each
test runs on whichever browser is present; the Chrome half needs the extra and
Google Chrome:

```bash
SUBLLM_BROWSER=1 uv run --extra chrome pytest -m browser
```

A second opt-in live suite covers **web-agent mode** — per client, a grounded
browsing turn (G1) and a negative control (G3) that tries to read a canary file,
a loopback address and a public-looking name that resolves to loopback, and
asserts none of it reaches the answer:

```bash
SUBLLM_LIVE=1 uv run pytest -m integration -k web_agent -v
```

The claude half additionally needs macOS `sandbox-exec`.

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
| `ChatGPTWebLLM` | `(mode="default", plan="plus", browser="auto", region_guard=None, timeout_s=None)` — `timeout_s` defaults to 2700 for `mode="pro"`, else 600; `mode="pro"` needs `plan="pro"` (`ValueError`); one send per call, no retry |
| `ChatGPTWebLLM.complete_result` | `(prompt) -> WebTextResult` |
| `WebTextResult` | `text, format ("markdown"\|"plain"), served_model, conversation_url` (frozen) |
| `CodexLLM.run_web_task` / `ClaudeLLM.run_web_task` | `(prompt, schema_model=None, *, tools="web", timeout_s=300) -> WebAgentResult` — one attempt, no retry |
| `ChatGPTWebLLM.generate_image` | `(prompt, out_dir) -> ImageResult` — one attempt, no retry, route `"web"` |
| `CodexLLM.generate_image` | `(prompt, out_dir, *, references=()) -> ImageResult` — one attempt, no retry; `references` are files attached with codex `-i` |
| `ImageResult` | `paths, route, text, conversation_url, served_model` (frozen) |
| `ImageGenerator` | abstract base with `generate_image(prompt, out_dir)` |
| `WebAgentResult` | `client, model, answer, trace, retrieved_urls, fetch_events_exposed, trace_complete, tools, client_version, duration_ms` (frozen; `.to_json()` gives the CLI document) |
| `TraceEvent` | `kind, raw_type, raw, query=None, url=None, final_url=None, ok=None, result_urls=()` (frozen) |

## Errors

All inherit `SubllmError`:

- `QuotaError` — subscription quota exhausted. The only default `FallbackLLM` trigger.
- `ClientError` — subprocess crash, missing binary, auth missing/invalid, tmux failure.
- `OutputError` — empty output, invalid JSON, or schema-validation mismatch.
- `RegionError` — preflight found the public IP out of region. Hard stop.
- `IsolationError` — web-agent mode only: the trace held an event outside web
  search/fetch, or a fetch of a non-public target. Carries `.trace`, never the
  answer. Hard stop, and **not** a `FallbackLLM` trigger — the other client is
  no safer.

Chat web route only (`ChatGPTWebLLM`):

- `HumanActionRequired` — a person must log in or solve a challenge before any
  send (CLI exit 3). Carries `.browser` and `.detail`.
- `ModelDowngradeError` — a `mode="pro"` run whose page names a non-Pro model.
  Carries `.text`, `.served_model`, `.requested_mode`; the answer is kept.
- `SendUnconfirmedError` — the send key was pressed but the outcome is unknown.
  The prompt is **never resent**; `.conversation_url` (or `None`) shows where to look.
- `QuotaError` from the web route is raised only before the send key. After the
  key, the same dialog is a `SendUnconfirmedError`.

`CodexLLM`/`ClaudeLLM` retry only transient failures (`ClientError`/`OutputError`)
within a single client with 1s/2s/4s backoff; cross-client failover is
`FallbackLLM`'s job alone. **`run_web_task` never retries** (see above).

## Hard constraint

Client-native execution only. subllm **never** builds or uses a
subscription-to-API HTTP proxy (a token wrapped behind an OpenAI-compatible
endpoint) — that rewrites request shapes, isn't client-native, and carries
account-ban risk. It only ever drives the real official clients: the official CLIs as subprocesses, and the
official chatgpt.com web app in a real logged-in browser through that browser's
own automation interface. Requests go only through the page's own UI. subllm runs
no HTTP server and replays no `backend-api` call.

The egress filter web-agent mode runs for the claude client is **not** that
proxy: it is a network-layer CONNECT filter that decides whether a TCP
destination may be reached. It terminates no TLS, reads and rewrites no token or
request, and speaks no vendor API — the client authenticates to Anthropic
itself, natively.

## Docs

- Design spec (PRD): [`docs/superpowers/specs/2026-06-02-subllm-v1-design.md`](docs/superpowers/specs/2026-06-02-subllm-v1-design.md)
- Implementation plan: [`docs/superpowers/plans/2026-06-02-subllm-v1.md`](docs/superpowers/plans/2026-06-02-subllm-v1.md)
- Subprocess contract (TS/Go ports): [`docs/drivers-contract.md`](docs/drivers-contract.md)
- Background research: [`docs/research/`](docs/research/)
