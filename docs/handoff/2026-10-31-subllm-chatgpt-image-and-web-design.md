# Handoff — subllm ChatGPT image generation and web route

Design: docs/superpowers/specs/2026-10-31-subllm-chatgpt-image-and-web-design.md

## Slice 1 — Codex-route image

### T1 — ImageResult/ImageGenerator, codex_exec collector, CodexLLM.generate_image

**Decisions**
- `run_codex_exec(collect_images=...)`: when set, always a fresh scratch home (auth.json only, copied from the caller's `codex_home` else $CODEX_HOME/~/.codex), fresh empty temp work dir (`-C`, `cwd`, `stdin=DEVNULL`), both removed in `finally`.
- Collector gets sorted regular non-symlink files under `<scratch>/generated_images/` only, after a zero exit and before scratch removal. Empty list is passed through; the CodexLLM collector raises OutputError.
- `generate_image` is ONE attempt, no `_retry`: `_retry` catches OutputError/ClientError, so a refusal or timeout would re-spend ~46 s and quota per attempt. Matches `run_web_task`.
- Names `<UTC ts>-<8 hex>-<n>.<ext>`, ext lowercased; `open(..., "xb")` so no overwrite. A collision raises FileExistsError (not wrapped; 32-bit random + second timestamp makes it near-impossible). (superseded by T2 fix: wrapped as ClientError)
- Fixed prompt wrapper is a module constant `_IMAGE_INSTRUCTION` (A-6); `search` not passed.
- `collect_images=None` path unchanged (same argv, no cwd/stdin, same codex_home handling).

**Rejected**
- `--json`/thread_id collection (spec D-2); diffing a shared home; wrapping with `_retry`.
- Wrapping FileExistsError/OSError into ClientError: speculative, left raw. (superseded by T2 fix: wrapped as ClientError)

**Assumptions**
- Real codex writes images to `$CODEX_HOME/generated_images/<thread>/exec-<id>.png` (spec D-2, verified live 2026-10-31); stub tests mirror that. Check: slice 1 live test (later task).
- `-C <dir>` is accepted by `codex exec` (same flag codex_web.py uses).

**Deferred**
- CLI `subllm image`, drivers-contract entry, live test: later tasks.
- Partial copy failure (ENOSPC/EACCES on file 2 of 2) leaves file 1 orphaned in out_dir with no ImageResult listing it; a name collision surfaces as raw FileExistsError (reviewer finding, parked as robustness). (superseded by T2 fix: wrapped as ClientError)

**Touches**
- python/src/subllm/base.py, __init__.py (public exports ImageResult, ImageGenerator), drivers/codex_exec.py, codex.py, tests/test_codex_image.py (new).
- Shared surface: `__init__.py` exports and `run_codex_exec` signature (new kwarg) — web-route tasks add to `__init__.py`.

### T2 — subllm image --route codex

**Decisions**
- `_run_image` in cli.py builds `CodexLLM()` defaults, prints one JSON line (`paths` as str, route, text, conversation_url, served_model) via `_json_document`/`_write_json_line`.
- Errors: `error: <ClassName>: <msg>` on stderr, rc 1, stdout empty. Handled inside `_run_image`, so the shared `complete` handler (`error: <msg>`) is untouched.
- Review fix: `generate_image` validates/creates out_dir (mkdir + W_OK) before codex starts and wraps copy OSError, both as ClientError (so a non-dir/unwritable -o gives `error: ClientError:` rc 1, no codex spend). An empty out_dir may now remain after OutputError/QuotaError; T1 quota test and the exclusive-create test (FileExistsError -> ClientError) updated.
- `--route` is argparse `choices=["codex"]` only; slice 3 must widen it to include "web". `-o/--out-dir` required.

**Rejected**
- Typed prefix in the shared handler: would change `complete`/`complete-json` stderr format.
- web-agent style typed JSON errors on stdout: spec §4.2 wants stderr for `image`.

**Assumptions**
- Prompt reaches codex via argv (`-- prompt`), so the stdin test checks argv in the stub.
- Non-SubllmError exceptions (e.g. FileExistsError/OSError) propagate as a traceback; check if slice 2 wants them mapped. (superseded by T2 fix: wrapped as ClientError)

**Deferred**
- Exit 3 / HumanActionRequired, --model/--effort/--mode/--plan/--browser flags: later slices.

**Touches**
- python/src/subllm/cli.py (new `image` subparser, docstring usage), python/tests/test_cli_image.py (new). Slice 3 collides on the `--route` choices and `_run_image`.

### T3 — live test and docs

**Decisions**
- Live test `test_generate_image_writes_decodable_png` (only `-k image` match); stdlib PNG check helper (signature, IHDR >0, IDAT zlib-decompresses). Asserts paths inside out_dir, route codex, url/model None.
- Live run once: PASSED, 47.5 s, 1 selected of 397. Image dimensions/file count not captured (test asserts >=1 file, decodable; no rerun to avoid quota spend).
- README: new "Image generation (Codex route)" subsection, CLI line + sentence, API rows, `-k image` command, hermetic count 386 passed / 11 skipped.
- drivers-contract: IMAGE COLLECTION bullet; KNOWN GAP reworded (Python passes --ephemeral always; -C/cwd/stdin-close only on image path).

**Assumptions**
- Count 386/11 includes the new live test as a skip; check with `cd python && uv run pytest -q`.

**Deferred**
- Web route docs, `--route web`: slices 2-3.

**Touches**
- python/tests/test_integration.py, README.md, docs/drivers-contract.md. Slice 3 collides on README CLI/API rows.

### Whole-run gate fixes

- Collector returns an empty list when `<scratch>/generated_images` is a symlink or not a dir (D-2 boundary).
- `_isolated_codex_home` removes its scratch dir and re-raises if the auth copy fails.
- `generate_image` resolves out_dir inside the ClientError wrapper (symlink loop -> ClientError naming out_dir).
- Pre-run out_dir check requires `W_OK | X_OK`.
- README image example shows an absolute path.

### Slice 1 gate
- Whole-run reviewer: PASS (README path minor and stale handoff lines fixed). Codex: 3 MAJOR + 2 MINOR, all fixed in 815ee5a; blocker-only re-check PASS.
- Full suite after fixes: 390 passed, 11 skipped. Live `-k image`: 1 passed (~47 s, one decodable PNG).
- Privacy scan of the slice range: clean (author-identity warning only).

implement-loop: slice 1 shipped 815ee5a; remaining: [2, 3, 4, 5]

## Slice 2 — Web-route text on ego lite

### T1 — page_core.js and browser fixture tests

**Decisions**
- `page_core.js` is one IIFE expression returning `{state, composerMatches, acceptance, turnStatus, captureText, servedModel}`; no window globals, no input events, no network.
- One turn element holds both halves (spec wording "assistant turn after turn_key" adapted): text comes from the `:assistant` unit's markdown body, so the prompt is never in `text`. The Copy button lives OUTSIDE the assistant block (action row is a sibling), so `copyButton` searches the whole turn for an exact `Copy` label and skips anything inside the user unit.
- `state().quota` added (pre-keypress QuotaError); `turnStatus(turn_key, prev_text)` takes `prev_text` for `text_stable`; `acceptance(url)` optional override for fixture tests; unknown/null key falls back to the last turn.
- `captureText` restores the original descriptors in `finally` (deletes the own property if the method was inherited), and creates/removes a stub `navigator.clipboard` when the page has none.
- Browser tests run one ego script for all fixtures (`SUBLLM_BROWSER=1`), 23 tests; in-page `pre_*` steps mutate the loaded DOM (fake clipboard, copy handlers, unhide Stop / user Copy message).
- Synthetic fixtures (first-line `SYNTHETIC` comment), built by `bs4` edits of the live captures: logged-out, challenge, quota-dialog, rejected-send, streaming, completed-hidden-stop. The three `redesign-*` live captures are untouched.

**Rejected**
- `/copy/i` Copy match: captures the prompt via "Copy message" (observed live); exact label plus user-unit exclusion instead (the regex alone is a redundant-guard mutant, see below).
- Scoping Copy to the climbed assistant block: the action row is not inside it (first run: copy_visible false).
- Fabricated legacy fixtures: none exist; legacy selectors are held but untested.

**Assumptions**
- The turn key stays the same when the placeholder becomes a server id (unverified, hence the last-turn fallback). Check with a live two-phase send in T-live.
- Streaming Stop and quota wording match the live page (synthetic only); replace `synthetic-streaming.html` with a live capture later.
- ego `page.evaluate(string)` awaits promises (verified by the captureText tests).

**Deferred**
- Legacy layout tests, `servedModel` on redesign (null until slice 5 audit), image `images` assertions with a real image (slice 3).

**Touches**
- New: `python/src/subllm/drivers/chatgpt_web/{__init__.py,page_core.js}`, `python/tests/test_chatgpt_web_core.py`, six `synthetic-*.html` fixtures. Shared: `python/pyproject.toml` (`browser` marker).
- Mutation battery: 10/10 behavioural mutants caught; `copy_regex_only` survives by design (the user-unit exclusion is a second guard; both removed together is caught).
- Also touched `python/tests/test_codex_web.py::test_fixtures_are_sanitized` (iterdir -> recursive files): the new `chatgpt_web/` subdir made `read_text()` raise IsADirectoryError; the chatgpt_web fixtures now get the same needle scan.

**Orchestrator additions (T1)**
- Live sends so far: 2 of 3. Send 1 (17:11) captured nothing usable from `document.querySelector("main")`: after the client-side route change it returned a stale home `main`. Send 2 ran `page_core.js` live at every phase: `state()` chat/logged in; a 5-line prompt with a blank line typed as segments joined by `Shift+Enter` matched `composerMatches` and did not send early; `acceptance()` went pending (`/c/local-chatgpt%3A<uuid>`, turn_key null) -> accepted at ~0.9 s; the turn key at acceptance equalled the key at completion (assumption above verified); `turnStatus` reached done at ~10 s; `captureText` returned `format: "markdown"` (bullet list intact); `servedModel` null; clipboard methods restored.
- After the Copy click the assistant button's label reads `Copied` for a moment; `redesign-completed-markdown.html` was captured in that state, so its test asserts stop/stability/text, not `done`.
- New live fixtures: `redesign-streaming-text.html` (rendered Stop, no Copy) and `redesign-completed-markdown.html`, both trimmed to the `thread-scroll-container` subtree (the raw capture root climbed to the sidebar; the sidebar was cut before sanitizing). `synthetic-streaming.html` stays for the user-"Copy message" test.
- `sanitize.py` (committed): allowlist text sanitizer for captures; drops `data-*name*/email` values (the home greeting carried the account name in `data-headline-name`).
- Pre-send tab handling: `Page.bringToFront` before `goto` (one goto without it never rendered the composer).

### T2 — errors and EgoDriver

**Decisions**
- `HumanActionRequired`, `SendUnconfirmedError`, `ModelDowngradeError` subclass `SubllmError` directly; exported from `subllm`.
- Protocol extensions to spec §4.3: a `rejected` event (before the client result when acceptance reads rejected); `format` and `conversation_url` on the ok line.
- Subprocess timeout = `timeout_s + 90 + int(len(prompt) * 0.06)`; the script deadline = `timeout_s` from script start (so the script normally reports first).
- Lock: `~/.local/share/subllm/chatgpt-web.lock` (flock, polled to `timeout_s`), held for the whole run, fd closed in `finally`.
- Keypress record: `mkdtemp(prefix="subllm-chatgpt-")/keypress`, written by the script before `SUBLLM_EVENT keypress`; dir removed in `finally`.
- `available()` caches only a passing detection; `check_guide()` is part of the ladder and callable on its own.
- Post-keypress `client` without a `rejected` event maps to SendUnconfirmedError (acceptance pending = unknown), only rejected+client is a plain ClientError.
- `downgrade` kind is mapped (requested_mode None) but the script never emits it yet (slice 5).

**Rejected**
- A mkdir in the script for the keypress dir: mkdtemp already creates it; fewer moving parts.
- Trusting ok:true on a non-zero exit: could be a half-dead runtime; SendUnconfirmedError per the brief.

**Assumptions**
- The ego runtime can write under the system temp dir (`node:fs`); unverified live. If not, the script fails before any keypress (safe), check on the first live run.
- `task.handOff()` exists in guide v2.2.0 (from the brief); the browser tests do not cover it. Check live.
- `ego-browser skill` prints `usage guide vX.Y.Z` (given in the brief).

**Deferred**
- `browser` resolution and WebTextResult wrapping (T3); downgrade detection (slice 5); images (slice 3). The generated script was only `node --check`ed, never run.

**Touches**
- New: `errors.py` additions, `__init__.py` exports, `drivers/chatgpt_web/{ego.py,ego_run.js}`, `tests/test_chatgpt_web_ego.py`. No page_core.js change.
- Mutation battery on ego.py mapping: 8/8 caught.

**Review fixes (T2)**
- Prompt normalized in `run_text` before the script is built: CRLF and lone CR become LF, tab becomes four spaces (keyboard.type presses Enter on CR and moves focus on Tab). Length-based budget uses the normalized prompt.
- "chat opened before the send key" branch now writes the keypress record first, so Python raises SendUnconfirmedError.
- Answer deadline starts right after the Enter press; page wait (45 s) and typing have their own bounds. Subprocess budget is now `timeout_s + 120 + int(len(prompt) * 0.06)`.
- `available()` is the ladder only (which, --version, health); a guide mismatch is only raised by `check_guide()` (spec D-4, never a fallback).
- Legacy composer: the script checks for the ProseMirror editor and clicks `#prompt-textarea` otherwise (untested live).
- Lock open, lock dir creation and template/core reads turn OSError into ClientError before anything is sent.

### T3 — ChatGPTWebLLM, CLI, docs, live test

**Decisions**
- `WebTextResult` lives in base.py; `EgoDriver.run_text` returns it directly, `EgoTextResult` deleted.
- `ChatGPTWebLLM` does not inherit `ImageGenerator` yet (slice 3). Driver is resolved lazily and cached; a failed resolve is not cached.
- Order in `complete_result`: region guard, resolve (detection), `check_guide`, `run_text`, empty-text check. Guide mismatch raises even under "auto".
- `complete_json` reuses codex `_JSON_INSTRUCTION` and `_parse_json_object` (imported, not copied); `complete_json_schema` stays the BaseLLM default.
- ego_run.js: `isChat()` early-send check now runs before `composerMatches`, so an early send always leaves the keypress record.
- CLI: `--effort` default is now None; "medium" is applied for codex in `_build_client`. Flag conflicts use `parser.error` (rc 2). chatgpt-web errors print `error: <Type>: <msg>`; HumanActionRequired exits 3; SendUnconfirmedError appends the conversation URL.
- Live test is gated on SUBLLM_LIVE and SUBLLM_WEB and `ego-browser` on PATH.

**Rejected**
- Raising stub `generate_image` now: dead code until slice 3.
- String-position test for the reorder: a node harness with a fake runtime runs the real generated script, so it proves behaviour. (Mutating the reorder is caught.)
- Chrome fallback under "auto" for a guide mismatch: spec D-4 forbids it.

**Assumptions**
- `node` is on PATH for the harness test (it skips otherwise).
- The README test count (451 passed, 37 skipped) is from the full run at the end of this task; re-measure if later tasks add tests.

**Deferred**
- `--mode`/`--plan` CLI flags and `mode != "default"` (slices 5); Chrome driver (slice 4); `plan` is validated and stored but not used yet.
- The README "Layout" section was not updated for new files.

**Touches**
- New: python/src/subllm/chatgpt_web.py, tests/test_chatgpt_web_llm.py, tests/test_cli_chatgpt_web.py.
- Changed: base.py (WebTextResult), __init__.py exports, cli.py (shared surface: `--effort` default, `--client`, `--browser`), ego.py, ego_run.js, test_chatgpt_web_ego.py, test_integration.py, README.md, AGENTS.md, docs/drivers-contract.md.

### T3 live-run fixes

- Live call 1 failed before the send (no quota): `page.goto` waits for `load` (15 s) and chatgpt.com stayed `interactive`. The script now uses `waitUntil: "domcontentloaded"` (probe: returns in ~9 s) and ignores a goto timeout whose message says the navigation committed; the 45 s state loop is the real wait. Any other goto error stays a pre-send `client` failure. Node-harness tests for both; 3/3 mutants caught.
- Live call 2: `SUBLLM_LIVE=1 SUBLLM_WEB=1 uv run pytest -m integration -k chatgpt_web` PASSED (23 s). Live call 3: `uv run subllm complete --client chatgpt-web "<one-token prompt>"` printed the token, rc 0 (21 s). No login wall or challenge.
- T2 assumptions discharged: the ego runtime writes the keypress record under the system temp dir (probe plus both live sends, which return ok only with the record present); `ego-browser skill` prints `usage guide v2.2.0` and `check_guide` passed live; `task.handOff()` is listed in the v2.2.0 guide (not exercised live: no login wall). Note: the guide says not to call `finish()` when a task stops for an error; the script follows spec D-4 (finish on every outcome except human_action).

### Slice 2 gate fixes

**Decisions**
- `errorText` skips an element if it is inside, or contains, a user message or assistant answer body. The "contains" half matters: a wrapper whose innerText starts with a prompt like "Something went wrong..." would otherwise match. The real "Unknown error" element is a leaf outside both, so synthetic-rejected-send is still rejected (browser-tested).
- The keypress record and event are written once, right before the first typed key; the old pre-Enter and early-chat record calls are gone. The `/c/` check still runs before `composerMatches` for the clearer detail; both now map to SendUnconfirmedError.
- Empty-after-normalize prompt is a ClientError in both `ChatGPTWebLLM.complete_result` (before detection) and `EgoDriver.run_text` (before the lock).
- `state().login_prompt` is the login control only. Human action needs `challenge` or `login_prompt`; otherwise `client` "chatgpt.com did not become ready", no hand-off. The wait loop also exits early on `login_prompt`.
- `fcntl` is imported inside `run_text`. Tested by AST (other stdlib/deps already put fcntl in sys.modules, so a sys.modules check cannot work).
- Fixtures added: synthetic-prompt-error-pending, synthetic-rejected-send-error-prompt, synthetic-completed-error-words.

**Rejected**
- Excluding only ancestors (`closest`): misses wrapper divs whose text starts with the prompt.
- Making the never-loaded node test wait 45 s: the fake runtime advances `Date.now` by 1 s per call instead.

**Deferred** (out of scope per the orchestrator)
- flock OSError other than EWOULDBLOCK; `WebTextResult.conversation_url` typing (str vs None); `finish()` after an error.

### Slice 2 gate fixes, cycle 2

- Whole-run reviewer re-check of 9e15e78: PASS. Codex re-check: its 3 findings fixed; one new MAJOR: excluding all of `[data-message-author-role=assistant]` hid a legacy error alert placed beside the answer body, so partial text could return as success. Fix: `errorText` excludes answer bodies only (`.markdown`, `assistant-message`), not the assistant wrapper. New fixture `synthetic-legacy-error-alert.html` + browser test (failed before the fix, passes after).
- Reviewer residual risk fixed: the state loop no longer ends on `login_prompt` (a login control can flash before the session hydrates and would have handed off a logged-in space). It waits for `logged_in`/`challenge`; `login_prompt` only classifies after the 45 s wait. Node-harness test for the flash case; mutant (break on login_prompt) caught.
- Harness tests that run out the 45 s wait now also make `setTimeout` immediate (ego tests 50 s -> 10 s).
- Supersedes the cycle-1 line "The wait loop also exits early on `login_prompt`".

### Slice 2 gate
- Whole-run reviewer: BLOCK (prompt text read as a page error) -> fixed in 9e15e78 -> PASS; cycle 2 (c6b19d7) PASS. Codex: 3 MAJOR (same error-text bug, early send before the record, module-level fcntl), all fixed; re-check found 1 MAJOR in the fix (assistant wrapper exclusion hid a legacy alert), fixed in c6b19d7. Two of two fix cycles used.
- Full suite at c6b19d7: 462 passed, 42 skipped. Browser fixture tests (SUBLLM_BROWSER=1): 30 passed. Live: opt-in test PASSED and one CLI call returned the token (before the gate fixes; the fixes touch only error classification, the pre-typing record, and the not-ready path).
- Privacy scan 6f702da..HEAD: clean (author-identity warning only).
- Deferred: flock OSError other than EWOULDBLOCK; `WebTextResult.conversation_url` typed str while `_map` can pass None; `finish()` after an error (guide advises against, spec D-4 keeps it); legacy answer without `.markdown` starting with an error phrase while the URL is a placeholder (reviewer: not a real scenario); the real chatgpt.com error placement is unverified live (a misplacement fails safe as SendUnconfirmedError).

implement-loop: slice 2 shipped c6b19d7; remaining: [3, 4, 5]

## Slice 3 — Web-route image on ego lite

### T1 — web-route image generation (driver + client)

**Decisions**
- One `ego_run.js` template, `ARGS.kind`/`ARGS.image_dir`; `build_script` defaults to text so old callers are unchanged.
- Image done = no Stop, images non-empty, equal to the previous read. Refusal grace 8 s counted from the first `ts.done && zero images` read, reset on any other read.
- `turnStatus.images` searches the whole turn plus the legacy assistant scope when it lies outside the turn; user units excluded via `closest`; `naturalWidth > 128`.
- `ego.py`: `_run`/`_run_locked` shared; `_map` now returns the ok payload dict (+ url); `run_text` builds `WebTextResult`. Image post-processing runs inside the `try` before `rmtree`. Validate all files before copying any (a bad second file saves nothing).
- Containment = not a symlink, regular file, `parent.resolve() == tmpdir.resolve()`; format from magic bytes, never from the listed name.
- `subllm/_images.py` (`prepare_out_dir`, `save_numbered`) shared with codex; codex behaviour identical.
- `ChatGPTWebLLM.generate_image`: guard -> empty prompt -> `prepare_out_dir` -> driver -> guide -> one attempt.
- Image subprocess budget +60 s.

**Rejected**
- Keeping `secrets`/`datetime` imports in codex.py just so the old test patch targets work: dead imports. Instead the two monkeypatch targets in `test_codex_image.py::test_exclusive_create_never_overwrites` moved to `subllm._images.*` (assertions unchanged).
- Requiring the Copy button for images: live fact says it is absent.
- Retrying image generation: duplicate quota spend.

**Assumptions**
- `page.fetch(blob:..., {saveAs})` works in the generated script as in the live probe; only a fake runtime covers it here (a live check belongs to T2/integration).
- An image-only turn has empty text; `captureText` fallback returns "" and that is allowed.

**Deferred**
- Live e2e, CLI flag, docs, integration test: T2.

**Touches**
- New: `python/src/subllm/_images.py`, `tests/fixtures/chatgpt_web/synthetic-completed-image.html`.
- Edited: `chatgpt_web.py`, `codex.py` (refactor only), `drivers/chatgpt_web/{ego.py,ego_run.js,page_core.js}`, tests `test_chatgpt_web_{core,ego,llm}.py`, `test_codex_image.py` (2 patch targets).
- Shared surface: `_map` return type (dict), `build_script` signature (new kwargs).

### T2 — `subllm image --route web`, opt-in live test, docs

**Decisions**
- One helper `_report_web_error(e) -> int` (stderr line, ` (conversation: <url>)` for SendUnconfirmedError, exit 3 for HumanActionRequired) now serves both `complete --client chatgpt-web` and `image --route web`; the codex image path keeps its own two-line handler, byte-identical.
- `--browser` on `image` is parsed for both routes and rejected after parsing for `--route codex` (`parser.error`, rc 2), like `complete`.
- Web CLI tests reuse the `egoimg` fixture from `test_chatgpt_web_llm.py` (real stub `ego-browser`, real file copy), so the CLI is exercised through the real driver.
- Live test accepts PNG or WebP (sniffed from the first bytes) with min side >= 256; `_assert_webp` reads VP8/VP8L/VP8X sizes (checked on synthetic headers for all three).

**Rejected**
- Routing the codex image errors through the web helper: would change the codex path (exit codes, URL suffix) for no gain.
- A shared client-builder for `image` and `complete`: two lines each, not worth an abstraction.

**Assumptions**
- The README claims (1254x1254, ~40 s, JPEG possible) come from the spec's live results and the driver's magic-byte list; JPEG has not been observed live.

**Deferred**
- `--mode`/`--plan` for image (slice 5). Live run of the new test (orchestrator).

**Touches**
- Edited: `python/src/subllm/cli.py`, `tests/test_cli_image.py` (dropped the `--route web` case from `test_argparse_errors`: the route is valid now, so argparse no longer rejects it), `tests/test_integration.py`, `README.md`, `docs/drivers-contract.md`.
- Shared surface: `image --route` choices and `--browser` flag.

**Orchestrator additions (T2): live results**
- Live call 1: `SUBLLM_LIVE=1 SUBLLM_WEB=1 uv run pytest -m integration -k "chatgpt_web and image"` PASSED in 44 s.
- Live call 2: `uv run subllm image --route web -o <scratch>` rc 0 in 42 s; one PNG, 1254x1254 RGB, about 1.1 MB, content matches the prompt; `text` was "" (image-only turn, no Copy capture), `served_model` null, `conversation_url` a server id. Output kept outside the repo.
- T1 assumption discharged: `page.fetch(blob:..., {saveAs})` into the per-call temp dir works in the generated script; containment and PNG sniff passed on the real file.
- Slice-2 gate fixes on the live send path: both sends returned ok, which the driver accepts only with the keypress record present, so the record-before-typing path works live. No false rejection or error dialog was read during either image turn (prompt had no error words, so the prompt-exclusion case was not stressed). No login wall or hand-off; whether a login flash happened is not observable from the result.
- A live completed-image DOM capture was not taken; `synthetic-completed-image.html` stands in.
- Quota note: a T2 CLI mutant (web route built as `CodexLLM`) ran one real `codex exec` image call before it was killed; its file was deleted. Mutation runs on the route branch now need a stub `codex` on PATH.

### Slice 3 gate fixes

- `ego.py::_collect_images`: `save_numbered`'s `ClientError` (copy failure after the send) is wrapped as `SendUnconfirmedError` with the conversation URL, per D-6. `_images.py` and the codex route are unchanged. `test_image_existing_target_is_never_overwritten` now expects it (the existing file stays untouched); new CLI test `test_web_write_failure_after_send_includes_url` checks `(conversation: <url>)` on stderr. Mutation (wrap removed) failed both tests.
- Live web image test also accepts JPEG via a stdlib `_assert_jpeg` (SOF0/1/2 size, checked on a synthetic header).
- README: the `-k image` line now says the web image test also needs `SUBLLM_WEB=1`; the failure-mode text and the drivers-contract IMAGE SAVE item list the failed write; counts now 503 passed / 44 skipped.

### Slice 3 gate
- Whole-run reviewer: BLOCK (a post-send copy failure into out_dir raised ClientError without the URL, against D-6) -> fixed in 6d015d2 -> blocker-only re-check PASS. One of two fix cycles used.
- Codex: 1 MAJOR (same write-failure finding) + 2 MINOR (live test rejected JPEG; README `-k image` line implied the web image test runs without SUBLLM_WEB=1), all fixed in 6d015d2. Not re-run: the fix only addresses its own findings.
- Full suite at 6d015d2: 503 passed, 44 skipped. Browser fixture tests (SUBLLM_BROWSER=1, at T1): 31 passed. Live: web image test PASSED (44 s); one CLI call saved a 1254x1254 PNG (42 s). Both live calls ran before 6d015d2, which changes only the post-send write-failure path.
- Privacy scan ad26598..HEAD: clean (author-identity warning only).
- Deferred: a live completed-image DOM capture (synthetic fixture stands in) and a live check of the stability rule against a progressive preview; a failed write of file 2 of 2 leaves file 1 in out_dir (the error carries the URL); a duplicated path in the script's `images` list would be copied twice (the script builds the list itself). Slice-2 deferred items remain open (flock OSError other than EWOULDBLOCK, `WebTextResult.conversation_url` typing, `finish()` after an error, real error placement on chatgpt.com).

implement-loop: slice 3 shipped 6d015d2; remaining: [4, 5]

## Slice 4 — Chrome fallback

### T1 — ChromeDriver

**Decisions**
- Seam deviates from the brief's `_launch_context(playwright, profile)`: `_open_context(profile)` is a contextmanager that owns `sync_playwright()` AND `launch_persistent_context(channel="chrome", headless=False, no_viewport=True)`, so stubs never start the patchright Node driver. The login spawn `_spawn_login()` is the second seam. The `login` helper (`chrome.main(["login"])`) goes through the same `_open_context`.
- Worlds (patchright 1.64.3 source: `Page.evaluate(..., isolated_context=True)` default): `call_core(page, name, *args, main_world=False)`. Main world only for `captureText` (clipboard override must see the page's Copy handler) and the image `fetch`; all DOM reads stay isolated (patchright's stealth). Expression string is built exactly like ego (`"(" + CORE + ")." + name + "(args)"`, no `arg`).
- `ego.py`: `_map` = `parse_output` + new `classify(events, results, code, sent, expired, tail, browser)`; `_map` kept as a thin wrapper. `chatgpt_lock(timeout_s)` contextmanager (lazy `import fcntl`) now serves both drivers (same path, messages, polling). Failure text uses `_NAMES[browser]` ("ego-browser" byte-identical for ego).
- Chrome builds in-process events/result dicts (`_Rec`, `_Stop`) and calls `classify(..., 0, rec.sent, None, "", "chrome")`; any non-`_Stop` exception becomes `kind client`, so pre-keypress = ClientError, post = SendUnconfirmedError (+accepted URL). `rec.sent = True` is set before the first key event.
- Human action: the context is closed (ExitStack exit) BEFORE `_spawn_login()`; spawn failure still raises HumanActionRequired with a "could not be opened" detail. The helper takes no lock; a retry while its window is open hits the profile-in-use ClientError (message names it).
- Image bytes: in-page `fetch` -> `FileReader.readAsDataURL` (no stack-limited `fromCharCode`) -> base64 returned -> `b64decode(validate=True)` -> `<tmpdir>/img-N` -> ego `_collect_images` unchanged.
- State loop tolerates evaluate exceptions (navigation destroying the context) until the 45 s end; ego would fail at once. Pre-send only, so safe.
- Clocks: module `_clock`/`_sleep` (tests patch them). Hermetic guard: autouse fixture in conftest patches both seams to `pytest.fail` unless the test has `browser`/`integration` marker.
- Deps: `uv add --optional chrome patchright` equivalent; env has `UV_FROZEN=1`, so lock needed `env -u UV_FROZEN`. `uv.lock` also moved `exclude-newer` 2026-05-26 -> 2026-10-25 (uv span rewrite); other changes are additions (patchright 1.64.3, greenlet, pyee).

**Rejected**
- Seam taking a `playwright` object (driver still starts in stubs). Importing patchright's `TimeoutError` (goto timeout detected by class name, no import needed). Isolated-world fetch of `blob:` (untestable offline; main world is the safe side). Spawning the login window as a child context of the run (Playwright kills Chrome when the parent exits).

**Assumptions** (T2 / live must verify)
- `captureText` in the main world sees the page's Copy handler; `blob:` fetch works from the main world; isolated-world DOM reads match the main-world DOM. None run offline.
- `ctx.wait_for_event("close", timeout=0)` fires when the user closes the window; `ctx.pages[0]` exists for a persistent context.
- `page.click(selector)` plus per-key `keyboard.type(delay=40)` is accepted by chatgpt.com like ego's (D-3a); unverified in Chrome.
- The detached helper can `import subllm` (same interpreter, installed package).

**Deferred**
- `_resolve_driver` fallback wiring, README, drivers-contract, CLI `--browser chrome` docs: T3. Browser-marker Chrome fixture tests: T2. A real-Chrome launch was never run (user decision).

**Touches**
- New: `drivers/chatgpt_web/chrome.py`, `tests/test_chatgpt_web_chrome.py`. Edited: `drivers/chatgpt_web/ego.py` (classify, chatgpt_lock; ego behaviour unchanged, no ego test edited), `tests/conftest.py` (autouse guard: affects every test), `pyproject.toml` (extra `chrome`), `uv.lock`.
- T3: `chatgpt_web.py` imports `EgoDriver, normalize_prompt` from ego; wire `ChromeDriver` there; `HumanActionRequired.browser` now "chrome" is possible (CLI handler for exit 3 should not assume "ego").
- Mutation battery: 8/8 caught (sent-after-typing, spawn-before-close, browser hardcoded, captureText isolated, image bytes reversed, rejected ignored, sent ignored, lock not taken).

**Review fixes (T1)**
- Image fetch is bounded: the in-page script uses an AbortController (30 s) around fetch and the blob read, so a stall rejects -> client -> SendUnconfirmedError and the lock is released (patchright `evaluate` has no default timeout).
- Login helper waits on the opened page's `close` event, then leaves `_open_context`, which closes the context so Chrome exits (on macOS the context `close` event only fires when the browser process quits).
- The three availability/launch tests inject fake `patchright` modules via `sys.modules`; verified by running the whole chrome test file with `sys.modules["patchright"] = None` (50 passed).
- Mutants: fetch bound removed and login wait reverted to the context were each caught by a new test.

### T2 — Chrome fixture tests

**Decisions**
- Module fixture `r` is parametrized over ["ego", "chrome"]; each skips on its own (ego-browser on PATH; patchright importable + `_chrome_executable()`). Module-level skipif now only checks SUBLLM_BROWSER. All 31 tests run on both from the one GROUPS table.
- Chrome runner: own `sync_playwright` + `launch_persistent_context(tmp profile, channel="chrome", headless=True)`, `ctx.route("**/*", abort)`, `goto about:blank` + `set_content`. Not `_open_context` (owned profile, headful).
- Worlds: `pre_*` steps and `CLIP_ROUNDTRIP` (composed, evaluated directly) -> main world; plain `call()` steps go through `chrome.call_core` (new `Call(str)` subclass carries name/args; the ego payload is unchanged) with `main_world=True` only for `captureText`; SETTLED and all other core calls -> isolated.
- Chrome-only tests: `_FETCH_JS` main-world fetch of the big image data: URL -> b64 decode == fixture payload + PNG signature; `call_core` captureText main vs isolated on a main-world Copy handler (main = markdown, isolated = plain).
- Result: no Chrome-vs-ego difference; all page_core checks pass on both. No chrome.py change. pyproject `browser` marker text updated.

**Rejected**
- Routing CLIP_ROUNDTRIP through call_core (it composes a clipboard-restore check around captureText, so it needs direct evaluation).
- Feeding the image through the driver's write helper: the decode/write is two inline lines in `_flow`, no helper exists; the test decodes with `b64decode(validate=True)` as the driver does.
- First mutation (call_core always isolated) was NOT caught by the 31 shared tests (clip_md uses the direct main-world expression; live_md_capture asserts plain, which both worlds satisfy) -> added the call_core captureText test above.

**Assumptions**
- Headless Chrome with `about:blank` + `set_content` is a non-secure context (`navigator.clipboard` absent), same as ego's document.write page; the FAKE_CLIPBOARD step handles it.
- Network abort covers everything (data: URLs are not routed); no fixture request was blocked in a way that failed a test.

**Deferred**
- Real chatgpt.com in Chrome (send path, copy handler there) stays unverified by user decision.

**Touches**
- `python/tests/test_chatgpt_web_core.py`, `python/pyproject.toml` (marker text only). Test count with SUBLLM_BROWSER=1: 64 (31 ego, 33 chrome).
- Mutants: call_core isolated -> caught by the new test (1/1); `_FETCH_JS` slice off by one -> caught by the image test (1/1). chrome.py restored (sha verified, git diff empty).

### T3 — browser=auto fallback, docs

**Decisions**
- `_resolve_driver`: chrome -> ChromeDriver only ("Chrome is not available: <reason>"); ego -> unchanged message; auto -> ego ladder, else ChromeDriver, both-fail message "no browser is available: ego lite is not available: <r>; Chrome is not available: <r>". Fallback decided only here; `check_guide()` and runs stay in `complete_result`/`generate_image` outside resolution, so a v3 guide or a run error never reaches Chrome.
- Importing `ChromeDriver` at module top is safe: chrome.py imports patchright lazily (checked with `sys.modules["patchright"]=None`).
- cli.py needed no change: `_report_web_error` is browser-agnostic (exit 3 for any HumanActionRequired); docstring still right.
- Tests: `FakeChrome` + `chrome` fixture in test_chatgpt_web_llm.py (imported by the two CLI test files); `test_ego_missing` now ego-only (auto + missing ego now selects the real ChromeDriver, which the conftest guard blocks).
- Full hermetic run: 574 passed, 77 skipped. Mutants 5/5 caught (a guide-mismatch fallback, b auto always Chrome, c auto never falls back, d drop Chrome reason, e retry Chrome after ego error).

**Rejected**
- Keeping `test_ego_missing[auto]`: it would launch the real fallback (guard fails it); replaced by fake-based fallback tests.
- Running `SUBLLM_BROWSER=1 ... -m browser` in this task (test hygiene forbids it); the command was only collected (`--extra chrome -m browser`: 64 tests). T2 ran it.

**Assumptions**
- The README browser command works as in T2's run; Chrome login flow / helper text describes chrome.py as read, never run live.

**Deferred**
- Live Chrome verification (user decision); stated plainly in README and drivers-contract.

**Touches**
- `python/src/subllm/chatgpt_web.py`, tests `test_chatgpt_web_llm.py`, `test_cli_image.py`, `test_cli_chatgpt_web.py`, `README.md`, `docs/drivers-contract.md`.

### Slice 4 gate fixes (cycle 1)

**Decisions**
- Overall limit: `threading.Timer` (daemon) armed after `_open_context` with `_budget()` (ego formula); on fire it sets `rec.dead`, records `Chrome timed out after Ns`, calls the `_kill_browser(profile)` seam. Cancelled in `finally`. `expired` goes to `classify` only when the flow did not return ok; the state loop re-raises once `rec.dead` (no 45 s spin on a dead browser).
- Pid source: profile-scoped `SingletonLock` symlink `<host>-<pid>` (Chromium's process singleton; patchright itself checks that file in `isProfileLocked`). Chosen over CDP `SystemInfo.getProcessInfo` because a frozen renderer may not answer CDP. Not verified against a running Chrome (no launch). Windows has no SingletonLock (`lockfile`), so the kill is a no-op there.
- `_chrome_executable()` mirrors patchright's channel list (darwin/linux fixed path, win32 env prefixes); `~/Applications` and PATH lookups removed. Reason now "Google Chrome is not installed at <path>; the subllm[chrome] driver needs it".
- Challenge hand-off says "solve the challenge"; launch errors mention the profile only when the text matches profile/singleton/in use/existing browser session.
- Conftest guard also patches `_kill_browser`.

**Deferred**
- Launch itself (`_open_context` enter) is not under the timer. Login helper ends when the first tab closes. Any flock OSError is treated as "lock held" and now affects both drivers.

**Touches**: chrome.py, conftest.py, test_chatgpt_web_chrome.py, test_chatgpt_web_llm.py, README.md, docs/drivers-contract.md. Mutants: 8/8 caught (timer not armed, wrong pid, no signal, expired dropped, ~/Applications accepted, reason without extra, challenge text, profile message always).
- Cycle 2: the timer callback runs its kill under a `threading.Lock` gate and the finish path takes the same gate (setting `finished`) before closing Chrome, so a callback can neither outlive the call nor kill a later call's browser; one test + one mutant (finish without the gate) caught.

### Slice 4 gate
- Owner decision (2026-11-01): offline verification only; the spec's "live text + image on Chrome" and the Chrome login prerequisite are waived, not blocked. The live Chrome path to chatgpt.com is unverified; README and drivers-contract say so.
- Whole-run reviewer: BLOCK (no overall time limit on a Chrome call, so a frozen renderer held the lock forever) -> fixed in 14db8d5 -> PASS; d96d7eb re-check PASS. Codex: 2 MAJOR (same time limit; detection accepted Chrome paths that `channel="chrome"` never launches) + 1 MINOR (Chrome-missing reason lacked `subllm[chrome]`), all fixed in 14db8d5; its re-check found 1 MAJOR in the fix (a late watchdog callback could kill the next call's browser), fixed in d96d7eb, re-check FIXED with no new defects. Two of two fix cycles used.
- Full hermetic suite at d96d7eb (tripwire stubs for codex/claude/ego-browser first on PATH, no SUBLLM_* env): 585 passed, 77 skipped; tripwire log empty for every run in this slice. Browser fixture tests (SUBLLM_BROWSER=1, real ego lite + headless Google Chrome via patchright, throwaway profile, all network aborted): 64 passed at dd5498d (ego 31, chrome 33); chrome substrate re-run at 14db8d5: 33 passed.
- Privacy scan 5928fcb..HEAD: clean (author-identity warning only).
- Deferred: the Chrome launch itself is not under the watchdog; the login helper ends when its first tab closes; any flock OSError is treated as "lock held" (now both drivers); the SingletonLock pid format and the kill are unverified against a running Chrome; Windows has no SingletonLock, so the watchdog kill is a no-op there; live Chrome text and image turns, main-world captureText and the blob: fetch on the real chatgpt.com page are unverified. Earlier slices' deferred items remain open.

implement-loop: slice 4 shipped d96d7eb; remaining: [5]

## Slice 5 — Modes and plan

### T1 — mode selection and downgrade audit in the web driver

**Decisions**
- `selectMode(target)` is a pure step planner (returns `{action: click|focus|key|done|fail, selector?, key?, current, selected_label?, detail?}`), not the spec table's "opens, selects, reads back; returns {ok, selected_label}". Reason: Radix opens on real pointer input and synthetic events are the known failure mode (D-3/D-3a), so the substrate performs each step with real input and re-asks. Read-back guarantee kept: only `done` (closed trigger label == target) counts as selected.
- Trigger label = last text-bearing LEAF element of the trigger, not the last `innerText` line. The sanitized Pro captures put the hidden "Thinking effort" placeholder inline, so `innerText` reads "Thinking effortPro" (no newline without the live CSS). DOM order (placeholder, then label) holds in all three captured states.
- `auditModel(mode, served)`: unverified (null/empty) / ok (mode != pro) / pro needs `\bpro\b` else downgrade. Substrates call it only when mode != default (default path stays byte-identical, no new evaluate).
- Substrates: `runPicker`/`_Picker.run` bounded 16 steps, 350 ms between; restore target = first step's `current`, set when it differs from the selected label or the selection failed (so an opened menu is closed again). Restore runs in ego `finally` before `finish` (skipped for human_action) and in chrome `_flow` `finally` (skipped when `rec.dead`); errors swallowed (ego logs a line).
- Picker runs after the pre-send checks and `isChat`, before composer focus and the keypress record; post-selection `state()` must be logged in / chat / not busy (quota -> `quota`).
- Downgrade: `kind downgrade` with `text`, `served_model`, `requested_mode` via Stop extra (ego) and a new `_Stop.extra` (chrome); audit runs right after `servedModel`, before any image fetch. `ego.classify` now passes `requested_mode`.
- `run_text/run_image/_run/_run_locked/_flow/build_script` take `mode="default"`; `_flow` split into `_flow` (restore wrapper) + `_flow_steps` (old body) to avoid re-indenting.
- Fixtures: `redesign-picker-{open-medium,open-pro,closed-pro}.html` sanitized with new UI_TEXT entries; residue reviewed (only generic UI strings; "/home" hits are Tailwind class names).

**Rejected**
- `data-selected-reasoning-effort` for readback (live: said medium while the label said Pro).
- Synthetic dispatch of pointer/key events from page_core (Radix ignores it; D-3a).
- A `quotaDialog` guard for `[role=menu]` descendants: the fixture test passes without it (the menu sits in no dialog/alert), so it was removed as unneeded code.
- `innerText` last line for the label (see Decisions).

**Assumptions**
- The live picker animation settles within the 350 ms step sleep, and the trigger shows one label at a time (no cross-fade of two leaves). Unverified; T3 live run checks. A cross-fade would make the planner see a wrong `current`; `done` could then be wrong only if the stale leaf equals the target.
- ego `page.focus(selector)` and `page.click(selector, {label})` exist as the brief states; chrome `page.focus` is standard patchright. Neither was run against the real chatgpt.com.
- Open-menu detection does not require `rendered` (a fading menu stays `data-state=open`), only connected and outside `[inert]`/`[aria-hidden=true]`; the trigger itself must be rendered and is read even inside aria-hidden main (Radix hides siblings while open).

**Deferred**
- `servedModel` for Pro turns (T3 after the live run). `chatgpt_web.py`/`cli.py` wiring of `mode` (T2). Mode with `image` kind has only fake-page coverage.

**Touches**
- `drivers/chatgpt_web/{page_core.js,ego_run.js,ego.py,chrome.py}`, tests `test_chatgpt_web_{core,ego,chrome}.py`, fixtures `redesign-picker-*.html`, `sanitize.py` (UI_TEXT).
- Shared surface: `ARGS.mode` in the generated script; driver method signatures gain `mode=`; `ego.classify` downgrade mapping; page_core exports `selectMode`, `auditModel`.

### T2 — ChatGPTWebLLM mode/plan/timeouts + CLI flags

**Decisions**
- Constructor order: mode set, plan set, browser set, then the pro gate, so a bad plan always gets the plan message. Gate text: `mode 'pro' needs plan='pro' (got 'plus')`. Timeout default 2700 s for pro, 600 s otherwise; explicit wins.
- CLI: `_web_mode_plan` shared by complete/complete-json/image. Plan = `--plan`, else `$SUBLLM_CHATGPT_PLAN` (empty counts as unset), else plus. An invalid env value errors only when the flag is absent and the web route is used (rc 2 naming the variable).
- Constructor ValueError -> `parser.error` inside the existing `try`; the `SystemExit` handler returns rc 2. `--mode/--plan` with codex/claude/`--route codex` -> parser.error, same wording pattern as `--browser`; `--browser` is checked first so its message is unchanged.
- FakeChrome now records `{timeout_s, mode}` per call; timeouts/mode asserted at the driver call.

**Rejected**
- Validating the env var for non-web clients (spec: env alone never errors there).
- Gate in the CLI instead of the constructor (library callers need it too).

**Assumptions**
- `ModelDowngradeError` prints via the existing `_report_web_error` as `error: ModelDowngradeError: ...` rc 1 (tested through ego stub, text and image). The downgrade text is not printed by the CLI (not asked).

**Deferred**: README/drivers-contract docs for the flags (not in T2 file list); live pro run (T3).

**Touches**: `python/src/subllm/{chatgpt_web,cli}.py`, tests `test_chatgpt_web_llm.py`, `test_cli_chatgpt_web.py`, `test_cli_image.py`. Shared: CLI flags `--mode/--plan`, env `SUBLLM_CHATGPT_PLAN`, `ChatGPTWebLLM` constructor contract. Mutants 10/10 caught.
