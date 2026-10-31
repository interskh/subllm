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
