# subllm ChatGPT image generation and web route — Design Spec (2026-10-31)

**Repo:** `python/` is authoritative. The TS SDK is not touched.
**Grounding:** `docs/research/research_chatgpt-web-automation_2026-10-31.md` (survey, risk, ego lite probes) and `docs/research/research_chatgpt-image-generation_2026-10-31.md` (Codex image verification; its route recommendation is superseded by this spec).
**Hard constraint (unchanged):** client-native execution only; no subscription-to-API proxy and no HTTP server in subllm.

## 1. Problem & goals

subllm runs text turns through `codex exec` and interactive `claude`. It cannot generate images, and it cannot use the ChatGPT **Chat** quota, which is separate from the Codex/Work quota and larger for the owner's use. The Chat surface also offers modes Codex does not expose, chiefly GPT Pro reasoning.

This spec adds two routes for ChatGPT work, chosen explicitly by the caller:

- **Codex route** — the genuine `codex exec`. Text already exists (`CodexLLM`); this adds images through Codex's built-in `image_gen` tool. Spends Codex/Work quota.
- **Web route** — the genuine chatgpt.com web app in a real, logged-in browser: **ego lite** (`ego-browser` CLI) when installed, otherwise **Chrome**. Text and images. Spends Chat quota only.

Goals, with the evidence that shows each is met:

- **G1. Codex-route images.** `CodexLLM.generate_image(prompt, out_dir)` returns the saved PNG paths.
  - **Evidence:** hermetic test with a stub `codex` that writes a PNG under its `CODEX_HOME/generated_images/<id>/`; the file is copied to `out_dir` and returned, and an exit with no PNG raises `OutputError`. Opt-in live test (`SUBLLM_LIVE=1`) produces a decodable PNG.
- **G2. Web-route text.** `ChatGPTWebLLM.complete` / `complete_json` return the assistant's Markdown answer from chatgpt.com Chat.
  - **Evidence:** hermetic tests with a stub `ego-browser` that emits the result protocol (§4.3), covering success, logged out, Cloudflare challenge, quota dialog, and a crash after send. Opt-in live test (`SUBLLM_LIVE=1 SUBLLM_WEB=1`) returns an answer containing a requested token.
- **G3. Web-route images.** `ChatGPTWebLLM.generate_image(prompt, out_dir)` saves the full-size image(s) of the current turn only.
  - **Evidence:** hermetic stub tests as in G2; live test produces a decodable PNG/WebP whose bytes differ from any image in an earlier turn of the same session.
- **G4. Chat quota only.** The web route never spends Codex/Work quota: it verifies the composer is in **Chat** mode (not Work) before every send and fails closed otherwise, and it never falls back to the Codex route by itself.
  - **Evidence:** DOM fixture test: a Work-mode fixture makes the pre-send check fail. Stub test: with a recording stub `codex` on PATH, a web-route quota failure raises `QuotaError` and the stub records no invocation.
- **G5. Browser selection.** `browser="auto"` uses ego lite when `ego-browser` is installed and healthy, else Chrome; `"ego"` / `"chrome"` force one and fail with `ClientError` if unavailable.
  - **Evidence:** hermetic tests of the detection ladder with stub binaries; live test on this Mac (ego lite) and on Chrome (subllm-owned profile).
- **G6. Modes and plan.** `mode="pro"` selects GPT Pro, verifies the selection before send, and audits the served model afterwards; `plan` (`free` | `plus` | `pro`, configurable, owner uses `pro`) rejects modes the plan lacks at construction time.
  - **Evidence:** fixture tests for pick-and-verify and for the downgrade audit; unit test that `mode="pro"` with `plan="plus"` raises `ValueError`; one live Pro turn.
- **G7. CLI.** `subllm complete --client chatgpt-web …` and `subllm image --route codex|web …` print text or one JSON document.
  - **Evidence:** hermetic CLI tests with stub binaries; typed errors map to a non-zero exit.

## 2. Non-goals

- **No silent route crossing.** Neither route falls back to the other. A caller who wants web-then-Codex text composes `FallbackLLM` explicitly, and the web route raises `QuotaError` only when nothing was sent (§3 D-7). `FallbackLLM` does not dispatch `generate_image` in this spec; image callers choose a route.
- **No backend replay.** subllm never calls `chatgpt.com/backend-api` from Python, Node, or a page-context `fetch` of an API path. In-page `fetch(img.src)` of an image the page already renders is allowed (it is the page's own resource).
- **No HTTP endpoint, no OpenAI-compatible shim**, and no reuse of `~/.codex/auth.json` outside the real `codex` binary. "Sign in with ChatGPT" plan usage is out (Codex/Work pool, no images).
- **Deep Research, Agent mode, and Work mode** are out of this spec (follow-up spec).
- **Reference images / image editing, aspect-ratio controls, and multi-image batches** are out. The prompt carries any size or ratio wording.
- **No CAPTCHA or Turnstile solving.** Challenges and logins hand off to the human.
- **No conversation deletion.** Deleting chats needs extra UI automation or a backend call; deferred (§7).
- **Existing calls do not change.** `CodexLLM.complete*`, `ClaudeLLM`, `FallbackLLM` defaults, and the CLI's existing flags keep their behaviour.
- **TS SDK parity** is out.

## 3. Architecture & decisions

```
caller
 ├─ CodexLLM.generate_image ── drivers/codex_exec.py ── codex exec (isolated CODEX_HOME)
 └─ ChatGPTWebLLM ── drivers/chatgpt_web/
       ├─ page_core.js        pure page functions, one copy for both browsers
       ├─ ego.py   EgoDriver  ── ego-browser nodejs  (ego lite)
       └─ chrome.py ChromeDriver ── patchright persistent context (Chrome)
```

### D-1 A separate `generate_image` capability, not a change to `complete`

`BaseLLM` stays text-in/text-out. A new abstract mixin `ImageGenerator` in `base.py` declares `generate_image(prompt, out_dir) -> ImageResult`. `CodexLLM` and `ChatGPTWebLLM` implement it; `ClaudeLLM` does not. *Rejected:* overloading `complete` to return paths (breaks every existing caller's type), and a standalone image client class per route (duplicates construction, region guard and errors).

### D-2 Codex route: same isolated run, collect PNGs before cleanup

`generate_image` reuses the isolated `codex exec` invocation (scratch `CODEX_HOME` with only `auth.json`, `--ignore-user-config`, `--ignore-rules`, `--ephemeral`, `project_doc_max_bytes=0`). The driver gains an optional collector: after a zero exit and before the scratch home is removed, it copies every new file under `$CODEX_HOME/generated_images/` into `out_dir`. "New" is a before/after listing diff, so a caller-supplied `codex_home` works too. The prompt wraps the caller's text with a fixed instruction to call the image generation tool once and reply with one word (verified 2026-10-31: without naming the tool, the agent may answer with SVG). Zero new files raises `OutputError`. *Rejected:* parsing `--json` for `thread_id` (no event carries the image; the directory diff is simpler and works for any home), and calling `backend-api/codex/responses` directly (the image-use `codex` backend pattern; violates the hard constraint).

**Verified 2026-10-31 with the production invocation** (scratch `CODEX_HOME` holding only `auth.json`, `-s read-only`, `--ignore-user-config`, `--ignore-rules`, `--ephemeral`, `project_doc_max_bytes=0`, empty stdin, codex-cli 0.165.0): exit 0 in 46 s, one 1536×1024 PNG at `$CODEX_HOME/generated_images/<thread_id>/exec-<call_id>.png`, 12,485 tokens (vs ~91k with the full home, which loads the imagegen skill). Codex also downloads plugin caches containing `preview.png` files into the scratch home, so the collector must look **only** under `generated_images/`.

### D-3 Web route: one page-side core, two thin substrates

All chatgpt.com knowledge lives in `page_core.js`: pure functions run inside the page through `evaluate`. Substrates only navigate, inject the core, call it, wait, and save bytes. *Rejected:* wrapping `steipete/oracle` and `leeguooooo/image-use` as subprocesses — neither can drive ego lite (it has no external CDP port or MCP; probed 2026-10-31), so the default path on this Mac needs subllm-owned code anyway, and wrapping them on Chrome would double the selector surface against a UI that changes every one to three weeks. Their MIT-licensed logic is ported with attribution (image completion and in-page byte fetch from image-use; text completion, Copy-button capture, and fail-closed mode selection from oracle).

`page_core.js` exports:

| Function | Contract |
| --- | --- |
| `state()` | `{logged_in, challenge, mode_surface: "chat" \| "work" \| "unknown", layout: "redesign" \| "legacy", busy}`. Logged in = composer present and no login button (the legacy profile-button test id is absent on the redesign; probe 2026-10-31). |
| `selectMode(mode)` | Opens the model picker, selects, reads the selection back; returns `{ok, selected_label}`. Never sends. |
| `fillAndSend(prompt)` | Verifies the exact editor text after the substrate types it (D-3a), then sends once; returns `{sent, turn_key, conversation_url}` only after the URL becomes a server id (`/c/<uuid>`). |
| `turnStatus(turn_key)` | `{done, text_stable, stop_visible, copy_visible, images: [src…], error_dialog}` scoped to the assistant turn after `turn_key`. `stop_visible` counts only a **rendered** Stop button: the composer keeps a hidden `aria-label="Stop"` button in the DOM after completion (observed 2026-10-31). |
| `captureText(turn_key)` | Markdown via the turn's own Copy button, captured in-page; falls back to `innerText`. |
| `servedModel(turn_key)` | The model label the page shows for that turn, or `null`. |

Selectors cover both layouts (`div.ProseMirror[role=textbox]` / `#prompt-textarea`; `data-test-id` / `data-testid`; `[data-turn-key]` / `conversation-turn-*`). Text completion is: no Stop control **and** a Copy control on the new turn **and** text unchanged across two reads. Image completion is: no Stop control **and** a new `<img>` inside the current turn, unchanged across two reads (no Copy requirement; an image-only turn may have none). A missing Stop control alone is never terminal.

### D-3a Input must look like a person typing; acceptance is the server id

Live run on 2026-10-31 in an ego lite agent space:
- **Inserting text without key events was rejected.** The composer was filled with `keyboard.insertText` and Send was clicked. The user turn rendered, but `POST backend-api/f/conversation/prepare` returned 403. The URL stayed at a local placeholder (`/c/local-chatgpt:<uuid>`), and the page showed "Unknown error" with a Retry button.
- **Per-key typing was accepted.** A fresh space brought the tab to front (`Page.bringToFront`; `visibilityState` visible, focused), typed with per-key events (`keyboard.type`, 40 ms delay) and pressed Enter. Every request returned 200, the URL became `/c/<uuid>`, and the image completed about 40 s later.

So the substrates must:
- bring the tab to front;
- type with per-key events and a delay, never `insertText` or paste;
- send with Enter.

A send counts as accepted only when the URL changes to a server id. "Unknown error" while the URL is still a `local-chatgpt:` placeholder means the server rejected the send before accepting it. That raises `ClientError` and is not resent; one changed variable (insert vs. type) separated failure from success, so treat this as the leading hypothesis, not proof.

The Chat/Work toggle is two buttons labelled `Chat` and `Work` with `aria-pressed`, and the composer shows an effort picker (it read "Medium").

**Web image retrieval verified 2026-10-31:** the result rendered as a `blob:` `<img>` (1254×1254) inside the turn's `[data-turn-key]` element, and `page.fetch(src, {saveAs})` saved a valid `image/png` (990 KB).

### D-4 ego lite driver: generated script over `ego-browser nodejs`

`EgoDriver` writes one Node script per call (SDK preamble + `page_core.js` source as a string + the call's JSON arguments, all inlined — the runtime does not inherit cwd or env) and pipes it to `ego-browser nodejs` on stdin. The script uses a fresh task space named `subllm-chatgpt-<8 hex>` (spaces share the profile's cookie jar, so a fresh space is logged in; a fixed name can collide with a space that became user-owned after a hand-off, as observed 2026-10-31). It calls `finish({keep: []})` on every outcome except a login wall or challenge; there it calls `handOff()`, leaves the space open for the user, and reports `human_action`. Image bytes are saved with `page.fetch(src, {saveAs: <absolute path>})`; Python resolves `out_dir` to an absolute path before generating the script.

Detection ladder for `browser="auto"`: `ego-browser` on PATH → `ego-browser --version` exits 0 → a no-browser health script (`console.log` of a sentinel) round-trips. Any failure selects Chrome. The guide version printed by `ego-browser skill` is checked against the major version the driver was written for (`2.x`); a mismatch is a `ClientError` naming both versions, never a guess.

### D-5 Chrome driver: patchright, subllm-owned profile

`ChromeDriver` uses `patchright` (optional extra `subllm[chrome]`) with a persistent context, `channel="chrome"`, headful, and a profile at `~/.local/share/subllm/chrome-profile`. First use (or a login wall) opens the page and raises `HumanActionRequired` telling the user to log in in that window. It runs the same `page_core.js` through `page.evaluate` and writes image bytes returned as base64. *Rejected:* attaching to the user's daily Chrome (Chrome 136+ ignores `--remote-debugging-port` on the default profile; the 144+ allow-dialog needs a click per session), the chrome-use extension (untrusted native-messaging install into the logged-in browser), nodriver (AGPL-3.0).

### D-6 One send per call; never resend

The web route does not use `_retry`. A call has two phases. **Before send** any failure is safe to report as `ClientError`, `QuotaError` or `HumanActionRequired`. **After send** (`fillAndSend` returned `sent`) any failure raises `SendUnconfirmedError` carrying the conversation URL; the caller can open it. Calls are serialized per machine with a file lock at `~/.local/share/subllm/chatgpt-web.lock` (the page rate-limits, and a parallel tab can receive another conversation's "Image created" toast).

### D-7 Errors and fallback eligibility

| Condition | Error | `FallbackLLM` default (`on=(QuotaError,)`) |
| --- | --- | --- |
| Rate-limit or usage-cap dialog **before** send | `QuotaError` | falls through |
| Send rejected before the server accepted it (URL still `local-chatgpt:`, "Unknown error") | `ClientError` (no resend) | propagates |
| Same dialog **after** send | `SendUnconfirmedError` | propagates |
| Logged out, Cloudflare/Turnstile challenge | `HumanActionRequired` | propagates |
| Composer in Work mode, mode selection unconfirmed | `ClientError` | propagates |
| Pro requested but a different model served | `ModelDowngradeError` (carries the answer and served model) | propagates |
| Browser/binary missing, script crash before send, guide-version mismatch | `ClientError` | propagates |
| Completed turn with empty text or no new image | `OutputError` | propagates |

`HumanActionRequired`, `SendUnconfirmedError` and `ModelDowngradeError` subclass `SubllmError` directly, so neither `_retry` nor `FallbackLLM` catches them by default.

### D-8 Modes and plan

`mode` is `"default"` (leave the picker; still verify Chat surface), `"thinking"`, or `"pro"`. The mapping from mode to picker labels lives in `page_core.js` and is the first thing updated when the picker changes; selection is read back and must match, else `ClientError` before send. `plan` defaults to `"plus"` and gates modes at construction: `"pro"` needs `plan="pro"`. With `mode="pro"`, after completion `servedModel` must name a Pro model; otherwise `ModelDowngradeError` (the Chat Pro cap reportedly downgrades silently; caps unconfirmed — research §risk). Pro turns run long, so `timeout_s` defaults to 2,700 s for `pro` and 600 s otherwise.

### D-9 Text turns use a regular chat by default

Temporary Chat disables image generation, and whether it offers Pro is unverified. Text and image turns both use a regular chat in v1. The account's memory and custom instructions can colour answers, the web analogue of Codex's AGENTS.md pollution; the README states this, and a `temporary=True` option is deferred until verified (§7).

## 4. Data contracts

### 4.1 Python

```python
# base.py
@dataclass(frozen=True)
class ImageResult:
    paths: list[Path]                 # saved files, in page/turn order
    route: Literal["codex", "web"]
    text: str                         # assistant text accompanying the image(s), may be ""
    conversation_url: str | None      # web route only
    served_model: str | None          # web route only, when the page shows it

class ImageGenerator(ABC):
    @abstractmethod
    def generate_image(self, prompt: str, out_dir: str | Path) -> ImageResult: ...

# codex.py
class CodexLLM(BaseLLM, ImageGenerator): ...   # constructor unchanged
#   image calls use timeout_s=max(self._timeout_s, 300)

# chatgpt_web.py
class ChatGPTWebLLM(BaseLLM, ImageGenerator):
    def __init__(
        self,
        mode: Literal["default", "thinking", "pro"] = "default",
        plan: Literal["free", "plus", "pro"] = "plus",
        browser: Literal["auto", "ego", "chrome"] = "auto",
        region_guard: RegionGuard | None = None,
        timeout_s: int | None = None,      # None -> 2700 for pro, else 600
    ) -> None: ...
#   region_guard.check() runs before any browser is touched (as in CodexLLM).
#   complete_json appends CodexLLM's JSON instruction and parses with the same
#   _parse_json_object; complete_json_schema keeps the BaseLLM default.

# errors.py — new
class HumanActionRequired(SubllmError): ...           # .browser, .detail
class SendUnconfirmedError(SubllmError): ...          # .conversation_url
class ModelDowngradeError(SubllmError): ...           # .text, .served_model, .requested_mode
```

Output files are named `<UTC yyyymmddThhmmss>-<8 hex>-<n>.<ext>` in `out_dir`, which is created if missing; existing files are never overwritten.

### 4.2 CLI

```
subllm complete      --client chatgpt-web [--mode default|thinking|pro] [--plan free|plus|pro] [--browser auto|ego|chrome] [PROMPT]
subllm complete-json --client chatgpt-web  …same flags…  [PROMPT]
subllm image         --route codex|web -o OUT_DIR [--mode …] [--plan …] [--browser …] [PROMPT]
```

`--plan` defaults to `$SUBLLM_CHATGPT_PLAN`, else `plus`. The existing `--effort` flag is rejected with `--client chatgpt-web` (the web route uses `--mode`); `--mode`, `--plan` and `--browser` are rejected with `--client codex|claude`. `image` prints one JSON object: `{"paths": [...], "route": "...", "text": "...", "conversation_url": ..., "served_model": ...}`. Errors print `error: <Type>: <message>` on stderr and exit 1; `HumanActionRequired` exits 3 so wrappers can prompt the user.

### 4.3 ego script result protocol

The generated script prints exactly these tagged lines (ego routes `console.log` to stderr; the parser reads stdout and stderr):

```
SUBLLM_EVENT {"sent": true, "conversation_url": "https://chatgpt.com/c/…"}     # at most once, right after send
SUBLLM_RESULT {"ok": true, "text": "…", "images": ["/abs/…"], "served_model": "…" | null}
SUBLLM_RESULT {"ok": false, "kind": "human_action" | "quota" | "client" | "output" | "downgrade", "detail": "…", "sent": bool, "conversation_url": … }
```

Exactly one `SUBLLM_RESULT` per run. Missing result + a `SUBLLM_EVENT sent` line → `SendUnconfirmedError`; missing both → `ClientError` with the tail of stderr. A non-zero exit with a valid result line trusts the result line. The Chrome driver returns the same dict shapes in-process.

### 4.4 Drivers contract

`docs/drivers-contract.md` gains `codex_exec` image collection (D-2) and a `chatgpt_web` section (D-3 to D-7, §4.3).

## 5. Slice plan

| # | Slice (user-visible) | Tasks | Subsystems | Prereq | Parallel |
| --- | --- | --- | --- | --- | --- |
| 1 | **Codex-route image, end to end.** `ImageResult`/`ImageGenerator`, `codex_exec` collector (only `generated_images/`), `CodexLLM.generate_image`, `subllm image --route codex`, stub + live tests, drivers-contract entry. | 3 | codex driver, CLI | — | no (shares `cli.py`, `__init__.py` with 2) |
| 2 | **Web-route text on ego lite.** `page_core.js` (`state`, `fillAndSend`, `turnStatus`, `captureText`; both layouts), `EgoDriver` (detection ladder, script generation, result protocol, hand-off, lock), `ChatGPTWebLLM.complete`/`complete_json` with `mode="default"`, new errors, `--client chatgpt-web`. Stub tests; browser fixture tests (§5.1); live test. At the size ceiling: if it overruns, split fixture capture and sanitization into its own slice 2a. | 3 | web driver (JS+ego), client/CLI | 1 | no |
| 3 | **Web-route image on ego lite.** Image detection and in-page `fetch` + `saveAs` in core/driver, `ChatGPTWebLLM.generate_image`, `subllm image --route web`. Fixture + live tests. | 2 | web driver, client/CLI | 2 | no |
| 4 | **Chrome fallback.** `ChromeDriver` (patchright extra, owned profile, login hand-off), `browser="auto"` fallback wiring, live text + image on Chrome. | 2 | chrome driver, client | 3 | no |
| 5 | **Modes and plan.** `selectMode`, `servedModel`, `plan` gating, downgrade audit, per-mode timeouts, CLI flags, README section on route choice and account risk. Fixture tests + one live Pro turn. | 3 | web driver, client/CLI | 4 | no (shares `ChatGPTWebLLM` with 4) |

### 5.1 Fixture tests

DOM fixtures are captured live from chatgpt.com states (logged out, idle Chat composer, idle Work composer, streaming text, completed text, completed image, quota dialog, picker open) for both layouts where the account shows them, then **sanitized**: synthetic prompts only; names, emails, avatars, conversation ids and signed URLs replaced. They are committed under `python/tests/fixtures/chatgpt_web/` with the capture date. Fixture tests load each file into a real browser page (ego lite if installed, else patchright Chromium) and call `page_core.js` functions; they carry an opt-in `browser` marker (`SUBLLM_BROWSER=1`) because CI has no browser. Python-side logic is covered by stub-binary tests that always run.

### 5.2 Live tests

`SUBLLM_LIVE=1` alone keeps running only the existing Codex/Claude suite plus the Codex image test. The web route additionally requires `SUBLLM_WEB=1`, because it spends Chat quota and carries account risk (§6 A-1).

## 6. Assumed decisions

- **A-1 Account risk accepted, opt-in only.** The owner reviewed the research (Terms clause against programmatic Output extraction; no recorded bans for paid accounts driven in a real browser) and asked for the web route. It is never on by default, never in a default fallback chain, and runs one call at a time.
- **A-2 Explicit routes over an `auto` route.** Quota pools and risk differ; the caller states the route. `FallbackLLM` remains the explicit composition tool.
- **A-3 patchright as the only Chrome substrate,** shipped as an optional extra so the base install stays `pydantic`-only.
- **A-4 Regular chats, no deletion, in v1** (D-9). History pollution in the owner's account is accepted until conversation cleanup lands.
- **A-5 `plan` defaults to `plus`.** A wrong `pro` setting would only cause pre-send failures, but a default that unlocks Pro for non-Pro users would be misleading. The owner sets `pro` via `SUBLLM_CHATGPT_PLAN=pro`.
- **A-6 Codex image instruction text is fixed,** not caller-tunable, because the agent picks SVG when the tool is not named.
- **A-7 ego guide major pinned to 2.x** (installed: v2.2.0). The hosted ego docs describe an older API; the installed `ego-browser skill` output is authoritative.
- **A-8 Private lock, not image-use's `~/.chatgpt-web.lock`.** Interop with third-party tools on the same account is deferred.

## 7. Deferred hardening

- Reattach to a `SendUnconfirmedError` conversation URL and finish capturing (`resume(url)`).
- Conversation cleanup through the UI menu; `temporary=True` for text once Temporary Chat's mode support is verified; or a dedicated ChatGPT Project, created through the UI, to keep subllm chats out of the main history (image-use's approach).
- Sweep stale `subllm-chatgpt-*` ego task spaces left open when a timed-out `ego-browser nodejs` is killed.
- Honour image-use's `~/.chatgpt-web.lock` for cross-tool serialization on one account.
- Typed handling of the specific Pro-cap dialog text, and reading remaining Pro quota from the page.
- Full-size originals via `estuary/content?…&p=fs` when the rendered `blob:` image is downscaled (check sizes in slice 3 first).
- Reference-image upload and editing on both routes (`codex exec -i`, composer file input).
- Deep Research and Agent mode (follow-up spec).
- TS SDK parity.

## 8. Review status

spec-review: pending
