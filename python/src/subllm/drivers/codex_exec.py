"""Invoke `codex exec` (ChatGPT-subscription auth) as a clean text/JSON engine.

Runs fully isolated to avoid the context-pollution gotcha (docs/research):
`--ignore-user-config` skips config.toml *only*, so on its own the user's global
`~/.codex/AGENTS.md` persona and the cwd's project `AGENTS.md` still leak in. We
close all of it — when no `codex_home` is given we run in a throwaway scratch
`CODEX_HOME` holding only a copy of the real `auth.json` (no global AGENTS.md,
config, rules, or skills), plus `--ignore-rules`, `--ephemeral`, and
`-c project_doc_max_bytes=0` to drop the cwd-discovered project AGENTS.md.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Sequence

from subllm.errors import ClientError, QuotaError

# stderr substrings that mean "out of subscription budget", not a bug.
# NOTE: "try again later" is intentionally NOT here — it is generic enough to
# appear in auth/network errors, and QuotaError is the ONLY exception FallbackLLM
# swallows. Misclassifying a real failure as quota would silently mask it.
_QUOTA_PATTERNS = re.compile(
    r"usage limit|rate.?limit|quota|too many requests",
    re.IGNORECASE,
)


def run_codex_exec(
    prompt: str,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    search: bool = False,
    codex_home: str | None = None,
    schema_path: str | None = None,
    timeout_s: int = 120,
    collect_images: Callable[[list[Path]], None] | None = None,
    images: Sequence[str | Path] = (),
) -> str:
    """Run one non-interactive codex turn. Returns the final message text.

    Raises QuotaError on subscription-limit stderr, ClientError otherwise.
    """
    with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False) as out_f:
        out_path = out_f.name
    scratch_home: str | None = None
    work_dir: str | None = None
    try:
        argv = [
            "codex", "exec",
            "-s", "read-only",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "-c", "project_doc_max_bytes=0",
            "-o", out_path,
        ]
        if model:
            argv += ["-m", model]
        if reasoning_effort:
            argv += ["-c", f'model_reasoning_effort="{reasoning_effort}"']
        if search:
            # NOTE: `--search` is NOT a valid `codex exec` flag in codex-cli
            # 0.136.0 (verified: `codex exec --search` -> "unexpected argument").
            # Web search for exec is enabled via config override instead.
            argv += ["-c", 'web_search="live"']
        if schema_path:
            argv += ["--output-schema", schema_path]
        for img in images:
            # absolute: codex runs in a throwaway work dir, not the caller's cwd
            argv += ["-i", str(Path(img).expanduser().resolve())]
        if collect_images:
            work_dir = tempfile.mkdtemp(prefix="subllm-codex-work-")
            argv += ["-C", work_dir]
        argv += ["--", prompt]  # -- so a prompt starting with '-' isn't parsed as a flag

        env = dict(os.environ)
        if codex_home and not collect_images:
            env["CODEX_HOME"] = codex_home
        else:
            scratch_home = _isolated_codex_home(codex_home)
            env["CODEX_HOME"] = scratch_home

        run_kwargs: dict = {}
        if collect_images:
            run_kwargs = {"cwd": work_dir, "stdin": subprocess.DEVNULL}
        try:
            proc = subprocess.run(
                argv, env=env, capture_output=True, text=True, timeout=timeout_s,
                **run_kwargs,
            )
        except FileNotFoundError as e:
            raise ClientError("codex binary not found on PATH") from e
        except subprocess.TimeoutExpired as e:
            raise ClientError(f"codex exec timed out after {timeout_s}s") from e

        if proc.returncode != 0:
            # Inspect stderr AND stdout — codex prints limit notices to either.
            blob = f"{proc.stderr or ''}\n{proc.stdout or ''}".strip()
            if _QUOTA_PATTERNS.search(blob):
                raise QuotaError(f"codex subscription limit: {blob[:200]}")
            raise ClientError(
                f"codex exec failed (exit {proc.returncode}): {_failure_text(blob)}"
            )

        if collect_images:
            # Only generated_images/: codex also caches plugin preview.png files
            # elsewhere in the scratch home.
            images_dir = Path(scratch_home) / "generated_images"
            files = [] if images_dir.is_symlink() or not images_dir.is_dir() else sorted(
                p for p in images_dir.rglob("*")
                if p.is_file() and not p.is_symlink()
            )
            collect_images(files)
        return Path(out_path).read_text()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
        if scratch_home:
            shutil.rmtree(scratch_home, ignore_errors=True)
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)


def _failure_text(blob: str, limit: int = 400) -> str:
    """The last `ERROR:` line codex printed, else the tail of its output.

    codex prints a session banner first, so the head of stderr never names the fault.
    """
    errors = [line for line in blob.splitlines() if line.startswith("ERROR:")]
    return (errors[-1] if errors else blob[-limit:])[:limit]


def _isolated_codex_home(source: str | None = None) -> str:
    """Throwaway CODEX_HOME holding only a copy of the real auth.json.

    Copy (not symlink) the current token so the run authenticates without
    dragging in the real home's AGENTS.md / config / rules / skills.
    """
    src = Path(source or os.environ.get("CODEX_HOME") or "~/.codex").expanduser()
    scratch = tempfile.mkdtemp(prefix="subllm-codex-")
    auth = src / "auth.json"
    try:
        if auth.exists():
            shutil.copy2(auth, Path(scratch) / "auth.json")
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    return scratch
