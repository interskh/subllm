"""Invoke `codex exec` (ChatGPT-subscription auth) as a clean text/JSON engine.

Runs read-only and config-isolated to avoid the AGENTS.md context-pollution
gotcha documented in docs/research.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

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
) -> str:
    """Run one non-interactive codex turn. Returns the final message text.

    Raises QuotaError on subscription-limit stderr, ClientError otherwise.
    """
    with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False) as out_f:
        out_path = out_f.name
    try:
        argv = [
            "codex", "exec",
            "-s", "read-only",
            "--skip-git-repo-check",
            "--ignore-user-config",
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
        argv += ["--", prompt]  # -- so a prompt starting with '-' isn't parsed as a flag

        env = dict(os.environ)
        if codex_home:
            env["CODEX_HOME"] = codex_home

        try:
            proc = subprocess.run(
                argv, env=env, capture_output=True, text=True, timeout=timeout_s
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
                f"codex exec failed (exit {proc.returncode}): {blob[:200]}"
            )

        return Path(out_path).read_text()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
