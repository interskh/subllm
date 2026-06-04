import json
import os
import pytest
from subllm.drivers.codex_exec import run_codex_exec
from subllm.errors import QuotaError, ClientError


def test_returns_last_message(fake_bin, tmp_path):
    # codex writes the final message to the path given after -o
    fake_bin("codex", r'''#!/bin/bash
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; shift; fi
  shift
done
printf 'a tidy summary' > "$out"
''')
    result = run_codex_exec("summarize this", codex_home=str(tmp_path))
    assert result == "a tidy summary"


def test_passes_output_schema_when_given(fake_bin, tmp_path):
    # codex echoes its own argv to a sidecar so the test can inspect flags
    argv_log = tmp_path / "argv.txt"
    fake_bin("codex", rf'''#!/bin/bash
echo "$@" > "{argv_log}"
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; shift; fi
  shift
done
printf '{{"ok": true}}' > "$out"
''')
    schema_file = tmp_path / "s.json"
    schema_file.write_text('{"type": "object"}')
    run_codex_exec("x", codex_home=str(tmp_path), schema_path=str(schema_file))
    argv = argv_log.read_text()
    assert "--output-schema" in argv
    assert str(schema_file) in argv


def test_passes_model_and_effort_flags(fake_bin, tmp_path):
    # -m / -c are the model-fidelity-critical flags; lock their argv wiring.
    argv_log = tmp_path / "argv.txt"
    fake_bin("codex", rf'''#!/bin/bash
echo "$@" > "{argv_log}"
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
printf 'ok' > "$out"
''')
    run_codex_exec("x", codex_home=str(tmp_path), model="gpt-5.4",
                   reasoning_effort="high")
    argv = argv_log.read_text()
    assert "-m gpt-5.4" in argv
    assert 'model_reasoning_effort="high"' in argv


def test_generic_try_again_later_is_client_error_not_quota(fake_bin, tmp_path):
    # A non-quota failure that merely says "try again later" (auth/network) must
    # NOT be misclassified as QuotaError — that is the only error FallbackLLM
    # swallows, so over-classifying would silently mask a real bug.
    fake_bin("codex", r'''#!/bin/bash
echo "Authentication failed. Please try again later." >&2
exit 1
''')
    with pytest.raises(ClientError):
        run_codex_exec("x", codex_home=str(tmp_path))


def test_quota_message_maps_to_quota_error(fake_bin, tmp_path):
    fake_bin("codex", r'''#!/bin/bash
echo "You have hit your usage limit. Try again later." >&2
exit 1
''')
    with pytest.raises(QuotaError):
        run_codex_exec("x", codex_home=str(tmp_path))


def test_missing_binary_maps_to_client_error(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))  # no codex here
    with pytest.raises(ClientError):
        run_codex_exec("x", codex_home=str(tmp_path))


def test_isolates_codex_home_when_none(fake_bin, tmp_path, monkeypatch):
    # No codex_home => run in a throwaway scratch CODEX_HOME (auth.json carried
    # in from the real home) so the user's global AGENTS.md / config / skills
    # never pollute the summary. --ignore-user-config alone did NOT do this:
    # it skips config.toml but leaves the global AGENTS.md persona intact, and
    # the cwd's AGENTS.md needs project_doc_max_bytes=0 on top. This locks the
    # full isolation contract the design intends.
    src_home = tmp_path / "real_codex"
    src_home.mkdir()
    (src_home / "auth.json").write_text('{"token": "abc"}')
    monkeypatch.setenv("CODEX_HOME", str(src_home))

    probe = tmp_path / "probe.txt"
    fake_bin("codex", rf'''#!/bin/bash
{{
  echo "HOME=$CODEX_HOME"
  echo "ARGV=$@"
  [ -f "$CODEX_HOME/auth.json" ] && echo "AUTH=yes" || echo "AUTH=no"
}} > "{probe}"
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
printf 'ok' > "$out"
''')

    run_codex_exec("summarize this")  # codex_home defaults to None

    seen = probe.read_text()
    scratch = seen.split("HOME=", 1)[1].splitlines()[0]
    assert scratch != str(src_home)              # ran in a throwaway, not real home
    assert "AUTH=yes" in seen                     # auth.json was carried in
    assert "--ignore-rules" in seen
    assert "--ephemeral" in seen
    assert "project_doc_max_bytes=0" in seen
    assert not os.path.exists(scratch)            # scratch cleaned up after the run
