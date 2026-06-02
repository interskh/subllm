"""Drive interactive `claude` in an ephemeral tmux pane and return the final
assistant text. Completion = tailing the JSONL session transcript (deterministic
filename via --session-id) for an assistant `end_turn` event that actually
contains TEXT — not a thinking-only end_turn, not pane scraping.

Faithful port of ralph-loop's ralph_lib/drivers.py, minus the bash-watchdog
stream-json emission. Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid as uuid_mod
from pathlib import Path

from subllm.errors import ClientError, OutputError, QuotaError

_BANNER_MARKERS = ("❯", "│ >")            # claude TUI input-prompt indicators
_TRUST_DIALOG_MARKER = "Do you trust the files in this folder"
_QUOTA_MARKERS = ("usage limit", "rate limit", "out of credit")
_PASTE_PLACEHOLDER_RE = re.compile(r"\[Pasted text #\d+")
_ENV_PASSTHROUGH = ("CLAUDE_CONFIG_DIR", "PATH", "HOME", "USER", "LANG", "TERM")


# --- pure completion logic (unit-tested) ------------------------------------

def _completed_text(jsonl_lines: list[str]) -> str | None:
    """Text of the LAST assistant `end_turn` event that contains text blocks, or
    None. Thinking-only end_turn events and partial streaming lines (stop_reason
    null) do NOT count — otherwise the turn would 'complete' before the real
    answer is written."""
    result: str | None = None
    for line in jsonl_lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message", {})
        if msg.get("stop_reason") != "end_turn":
            continue
        text = "".join(
            b.get("text", "") for b in msg.get("content", []) if b.get("type") == "text"
        )
        if text:                       # require actual text, not thinking-only
            result = text
    return result


def _turn_complete(jsonl_lines: list[str]) -> bool:
    return _completed_text(jsonl_lines) is not None


def extract_final_text(jsonl_lines: list[str]) -> str:
    text = _completed_text(jsonl_lines)
    if text is None:
        raise OutputError("no completed text-bearing assistant turn in transcript")
    return text


# --- deterministic transcript path + command builder (unit-tested) ----------

def _project_jsonl_path(config_dir: str, work_dir: str, session_id: str) -> Path:
    """claude encodes the *resolved* cwd into the project dir name, replacing
    every non-alphanumeric/non-hyphen char with '-'. With --session-id the
    filename is exactly <session-id>.jsonl (no fragile newest-file globbing)."""
    encoded = re.sub(r"[^A-Za-z0-9-]", "-", str(Path(work_dir).resolve()))
    return Path(config_dir) / "projects" / encoded / f"{session_id}.jsonl"


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _build_claude_cmd(session_id: str, model: str | None, permission_mode: str,
                      tools: str | None) -> str:
    """ONE shell-command string for tmux's init command (tmux runs it via sh -c).
    Passing split argv after the window flags is mis-parsed by tmux."""
    parts = ["claude",
             f"--session-id {_q(session_id)}",
             f"--permission-mode {_q(permission_mode)}"]
    if model:
        parts.append(f"--model {_q(model)}")
    if tools is not None:
        parts.append(f"--tools {_q(tools)}")   # tools='' disables tools
    return " ".join(parts)


# --- tmux plumbing ----------------------------------------------------------

def _tmux(*args: str, timeout: float = 5.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["tmux", *args], capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError as e:
        raise ClientError("tmux binary not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise ClientError(f"tmux command timed out: {' '.join(args)}") from e


def _capture(session: str) -> str:
    return _tmux("capture-pane", "-p", "-t", session).stdout


def _quota_banner(pane: str) -> bool:
    low = pane.lower()
    return any(s in low for s in _QUOTA_MARKERS)


def _env_args(config_dir: str) -> list[str]:
    """`-e KEY=VAL` pairs so the tmux/claude process writes its transcript where
    we read it. CLAUDE_CONFIG_DIR is the critical one."""
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = config_dir
    out: list[str] = []
    for k in _ENV_PASSTHROUGH:
        v = env.get(k)
        if v is not None:
            out += ["-e", f"{k}={v}"]
    return out


def run_claude_tmux(prompt: str, *, model: str | None = None,
                    permission_mode: str = "bypassPermissions",
                    tools: str | None = "", work_dir: str | None = None,
                    config_dir: str | None = None, timeout_s: int = 300,
                    poll_interval_s: float = 1.0) -> str:
    """Run one interactive claude turn via tmux; return final assistant text.
    tools='' (default) disables tools so an unattended turn never deadlocks on a
    tool-approval prompt. Raises QuotaError / ClientError / OutputError."""
    work_dir = work_dir or os.getcwd()
    config_dir = config_dir or os.environ.get(
        "CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")
    )
    session_id = str(uuid_mod.uuid4())
    session = f"subllm-{session_id[:8]}"
    jsonl_path = _project_jsonl_path(config_dir, work_dir, session_id)
    claude_cmd = _build_claude_cmd(session_id, model, permission_mode, tools)

    res = _tmux("new-session", "-d", "-s", session, "-x", "200", "-y", "50",
                "-c", work_dir, *_env_args(config_dir), claude_cmd)
    if res.returncode != 0:
        raise ClientError(f"tmux new-session failed: {res.stderr.strip()[:200]}")
    try:
        _wait_for_banner(session, timeout_s=30)
        _send_prompt(session, prompt)
        return extract_final_text(_await_completion(
            session, jsonl_path, timeout_s=timeout_s, poll_interval_s=poll_interval_s
        ))
    finally:
        try:
            _tmux("kill-session", "-t", session, timeout=3)
        except ClientError:
            pass  # cleanup failure must never mask the turn's real result/error


def _wait_for_banner(session: str, timeout_s: float) -> None:
    """Poll until the input prompt appears. On first run in a project the TUI
    shows a workspace-trust dialog first; accept it (send '1' + Enter) and keep
    polling — its own selection marker would otherwise be mistaken for the
    prompt."""
    deadline = time.monotonic() + timeout_s
    trust_handled = False
    while time.monotonic() < deadline:
        pane = _capture(session)
        if _quota_banner(pane):
            raise QuotaError(f"claude limit banner: {pane.strip()[:200]}")
        if not trust_handled and _TRUST_DIALOG_MARKER in pane:
            _tmux("send-keys", "-t", session, "1")
            time.sleep(0.2)
            _tmux("send-keys", "-t", session, "Enter")
            trust_handled = True
            time.sleep(1.0)
            continue
        if _TRUST_DIALOG_MARKER not in pane and any(m in pane for m in _BANNER_MARKERS):
            return
        time.sleep(0.5)
    raise ClientError("claude TUI prompt did not appear in time")


def _send_prompt(session: str, prompt: str) -> None:
    """send-keys -l for small single-line prompts; load-buffer + paste-buffer for
    multiline or >4KB (summaries routinely exceed 4KB). After a paste, wait for
    the bracketed-paste placeholder before Enter to avoid a dropped submit."""
    if "\n" in prompt or len(prompt.encode("utf-8")) > 4096:
        buf = f"subllm-{uuid_mod.uuid4().hex[:8]}"
        try:
            subprocess.run(["tmux", "load-buffer", "-b", buf, "-"], input=prompt,
                           text=True, check=True, timeout=5)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            raise ClientError(f"tmux load-buffer failed: {e}") from e
        _tmux("paste-buffer", "-p", "-d", "-b", buf, "-t", session)
        _wait_for_paste_placeholder(session)
    else:
        _tmux("send-keys", "-t", session, "-l", prompt)
        time.sleep(0.3)
    _tmux("send-keys", "-t", session, "Enter")


def _wait_for_paste_placeholder(session: str, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _PASTE_PLACEHOLDER_RE.search(_capture(session)):
            return
        time.sleep(0.1)
    # don't raise: pressing Enter anyway is cheaper than a full retry


def _await_completion(session: str, jsonl_path: Path, *, timeout_s: int,
                      poll_interval_s: float) -> list[str]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if jsonl_path.exists():
            lines = jsonl_path.read_text(errors="replace").splitlines()
            if _turn_complete(lines):
                return lines
        if _quota_banner(_capture(session)):
            raise QuotaError("claude limit banner during generation")
        time.sleep(poll_interval_s)
    raise ClientError(f"claude turn did not complete within {timeout_s}s")
