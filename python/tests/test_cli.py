import json
import pytest
from subllm.cli import main


def test_complete_prints_text(fake_bin, tmp_path, capsys, monkeypatch):
    fake_bin("codex", r'''#!/bin/bash
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
printf 'cli summary' > "$out"
''')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    rc = main(["complete", "--client", "codex", "summarize this"])
    assert rc == 0
    assert "cli summary" in capsys.readouterr().out


def test_unknown_client_errors(capsys):
    rc = main(["complete", "--client", "bogus", "x"])
    assert rc != 0
