# Handoff — subllm ChatGPT image generation and web route

Design: docs/superpowers/specs/2026-10-31-subllm-chatgpt-image-and-web-design.md

## Slice 1 — Codex-route image

### T1 — ImageResult/ImageGenerator, codex_exec collector, CodexLLM.generate_image

**Decisions**
- `run_codex_exec(collect_images=...)`: when set, always a fresh scratch home (auth.json only, copied from the caller's `codex_home` else $CODEX_HOME/~/.codex), fresh empty temp work dir (`-C`, `cwd`, `stdin=DEVNULL`), both removed in `finally`.
- Collector gets sorted regular non-symlink files under `<scratch>/generated_images/` only, after a zero exit and before scratch removal. Empty list is passed through; the CodexLLM collector raises OutputError.
- `generate_image` is ONE attempt, no `_retry`: `_retry` catches OutputError/ClientError, so a refusal or timeout would re-spend ~46 s and quota per attempt. Matches `run_web_task`.
- Names `<UTC ts>-<8 hex>-<n>.<ext>`, ext lowercased; `open(..., "xb")` so no overwrite. A collision raises FileExistsError (not wrapped; 32-bit random + second timestamp makes it near-impossible).
- Fixed prompt wrapper is a module constant `_IMAGE_INSTRUCTION` (A-6); `search` not passed.
- `collect_images=None` path unchanged (same argv, no cwd/stdin, same codex_home handling).

**Rejected**
- `--json`/thread_id collection (spec D-2); diffing a shared home; wrapping with `_retry`.
- Wrapping FileExistsError/OSError into ClientError: speculative, left raw.

**Assumptions**
- Real codex writes images to `$CODEX_HOME/generated_images/<thread>/exec-<id>.png` (spec D-2, verified live 2026-10-31); stub tests mirror that. Check: slice 1 live test (later task).
- `-C <dir>` is accepted by `codex exec` (same flag codex_web.py uses).

**Deferred**
- CLI `subllm image`, drivers-contract entry, live test: later tasks.
- Partial copy failure (ENOSPC/EACCES on file 2 of 2) leaves file 1 orphaned in out_dir with no ImageResult listing it; a name collision surfaces as raw FileExistsError (reviewer finding, parked as robustness).

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
- Non-SubllmError exceptions (e.g. FileExistsError/OSError) propagate as a traceback; check if slice 2 wants them mapped.

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
