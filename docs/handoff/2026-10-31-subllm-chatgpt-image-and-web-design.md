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
