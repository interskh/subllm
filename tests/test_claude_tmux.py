import json
from pathlib import Path
import pytest
from subllm.drivers.claude_tmux import (
    extract_final_text, _turn_complete, _project_jsonl_path, _build_claude_cmd,
)
from subllm.errors import OutputError


def _assistant(text=None, stop_reason=None, thinking=False):
    content = ([{"type": "thinking", "thinking": "hmm"}] if thinking
               else [{"type": "text", "text": text or ""}])
    return json.dumps({"type": "assistant",
                       "message": {"content": content, "stop_reason": stop_reason}})


def test_ignores_partial_streaming_lines():
    lines = [_assistant("partial", None), _assistant("the answer", "end_turn")]
    assert extract_final_text(lines) == "the answer"


def test_thinking_only_end_turn_is_not_complete():
    # Claude emits a thinking-only end_turn BEFORE the real text response.
    lines = [_assistant(stop_reason="end_turn", thinking=True)]
    assert _turn_complete(lines) is False
    with pytest.raises(OutputError):
        extract_final_text(lines)


def test_picks_text_end_turn_after_thinking_end_turn():
    lines = [
        _assistant(stop_reason="end_turn", thinking=True),  # thinking-only -> skip
        _assistant("real answer", "end_turn"),              # text response -> use
    ]
    assert _turn_complete(lines) is True
    assert extract_final_text(lines) == "real answer"


def test_concatenates_multiple_text_blocks():
    line = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "part one. "},
        {"type": "text", "text": "part two."},
    ], "stop_reason": "end_turn"}})
    assert extract_final_text([line]) == "part one. part two."


def test_no_completed_turn_raises():
    with pytest.raises(OutputError):
        extract_final_text([_assistant("still thinking", None)])


def test_project_jsonl_path_encoding(tmp_path):
    # non-alphanumerics (slashes, dots) -> '-'; filename is <session-id>.jsonl
    p = _project_jsonl_path(str(tmp_path / "cfg"), "/Users/example/git/x.y", "sess123")
    assert p.name == "sess123.jsonl"
    assert p.parent.name == "-Users-example-git-x-y"
    assert p.parent.parent.name == "projects"


def test_build_claude_cmd_is_single_string_with_session_and_disabled_tools():
    cmd = _build_claude_cmd("sess123", model="claude-x",
                            permission_mode="bypassPermissions", tools="")
    assert isinstance(cmd, str)
    assert "--session-id" in cmd and "sess123" in cmd
    assert "--permission-mode" in cmd and "bypassPermissions" in cmd
    assert "--model" in cmd and "claude-x" in cmd
    assert "--tools ''" in cmd  # tools disabled -> no approval deadlock


_FAKE_CLAUDE = r'''#!/usr/bin/env python3
import os, re, sys, json
from pathlib import Path
args = sys.argv[1:]
sid = ""
for i, a in enumerate(args):
    if a == "--session-id" and i + 1 < len(args):
        sid = args[i + 1]
cfg = os.environ["CLAUDE_CONFIG_DIR"]            # KeyError if -e didn't pass it
encoded = re.sub(r"[^A-Za-z0-9-]", "-", str(Path.cwd().resolve()))
d = Path(cfg) / "projects" / encoded
d.mkdir(parents=True, exist_ok=True)
rec = {"type": "assistant",
       "message": {"content": [{"type": "text", "text": "tmux answer"}],
                   "stop_reason": "end_turn"}}
(d / (sid + ".jsonl")).write_text(json.dumps(rec) + "\n")
'''

_FAKE_TMUX = r'''#!/usr/bin/env python3
import os, sys, subprocess
a = sys.argv[1:]
sub = a[0] if a else ""
if sub == "new-session":
    rest, workdir, env = a[1:], None, dict(os.environ)
    i = 0
    while i < len(rest):
        if rest[i] == "-c" and i + 1 < len(rest):
            workdir = rest[i + 1]; i += 2; continue
        if rest[i] == "-e" and i + 1 < len(rest):
            k, _, v = rest[i + 1].partition("="); env[k] = v; i += 2; continue
        i += 1
    shell_cmd = rest[-1]
    assert "claude" in shell_cmd, "expected ONE claude command string, got: " + repr(shell_cmd)
    assert "CLAUDE_CONFIG_DIR" in env, "CLAUDE_CONFIG_DIR was not passed via -e"
    subprocess.Popen(["sh", "-c", shell_cmd], cwd=workdir, env=env)
    sys.exit(0)
if sub == "capture-pane":
    sys.stdout.write("❯ ")   # banner marker so _wait_for_banner proceeds
    sys.exit(0)
sys.exit(0)   # send-keys / load-buffer / paste-buffer / kill-session -> no-op
'''


def test_run_claude_tmux_happy_path(fake_bin, tmp_path):
    config_dir = tmp_path / "cfg"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    fake_bin("claude", _FAKE_CLAUDE)
    fake_bin("tmux", _FAKE_TMUX)
    from subllm.drivers.claude_tmux import run_claude_tmux
    out = run_claude_tmux("summarize", work_dir=str(work_dir),
                          config_dir=str(config_dir), timeout_s=10,
                          poll_interval_s=0.1)
    assert out == "tmux answer"
