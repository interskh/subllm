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
