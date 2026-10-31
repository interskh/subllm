import io
import json
from pathlib import Path

import pytest

from subllm.cli import main

_WRITE = r'''
mkdir -p "$CODEX_HOME/generated_images/t1"
printf 'PNGDATA' > "$CODEX_HOME/generated_images/t1/exec-1.png"
'''


def _stub(fake_bin, body):
    fake_bin("codex", r'''#!/bin/bash
out=""
[ -n "$ARGV_LOG" ] && printf '%s\n' "$@" > "$ARGV_LOG"
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
''' + body + '''
printf 'done' > "$out"
''')


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    (h / "auth.json").write_text("AUTH")
    monkeypatch.setenv("CODEX_HOME", str(h))
    return h


def test_success_json(fake_bin, home, tmp_path, capsys):
    _stub(fake_bin, _WRITE)
    out = tmp_path / "out"
    rc = main(["image", "--route", "codex", "-o", str(out), "a cube"])
    cap = capsys.readouterr()
    assert rc == 0 and cap.err == ""
    assert cap.out.count("\n") == 1
    doc = json.loads(cap.out)
    assert set(doc) == {"paths", "route", "text", "conversation_url", "served_model"}
    assert doc["route"] == "codex" and doc["text"] == "done"
    assert doc["conversation_url"] is None and doc["served_model"] is None
    assert len(doc["paths"]) == 1 and isinstance(doc["paths"][0], str)
    saved = list(out.iterdir())
    assert len(saved) == 1 and saved[0].resolve() == Path(doc["paths"][0])
    assert saved[0].read_bytes() == b"PNGDATA"


def test_prompt_from_stdin(fake_bin, home, tmp_path, capsys, monkeypatch):
    log = tmp_path / "argv.txt"
    monkeypatch.setenv("ARGV_LOG", str(log))
    _stub(fake_bin, _WRITE)
    monkeypatch.setattr("sys.stdin", io.StringIO("stdin prompt"))
    rc = main(["image", "--route", "codex", "-o", str(tmp_path / "o")])
    assert rc == 0
    assert len(json.loads(capsys.readouterr().out)["paths"]) == 1
    assert "stdin prompt" in log.read_text()


def test_no_image_is_typed_error(fake_bin, home, tmp_path, capsys):
    _stub(fake_bin, "")
    rc = main(["image", "--route", "codex", "-o", str(tmp_path / "o"), "x"])
    cap = capsys.readouterr()
    assert rc == 1 and cap.out == ""
    assert cap.err.startswith("error: OutputError: ")


def test_quota_is_typed_error(fake_bin, home, tmp_path, capsys):
    _stub(fake_bin, 'echo "usage limit reached" >&2\nexit 1\n')
    rc = main(["image", "--route", "codex", "-o", str(tmp_path / "o"), "x"])
    cap = capsys.readouterr()
    assert rc == 1 and cap.out == ""
    assert cap.err.startswith("error: QuotaError: ")


@pytest.mark.parametrize("argv", [
    ["image", "--route", "web", "-o", "o", "x"],
    ["image", "--route", "codex", "x"],
    ["image", "-o", "o", "x"],
])
def test_argparse_errors(argv):
    assert main(argv) == 2


def test_complete_error_format_unchanged(fake_bin, tmp_path, capsys, monkeypatch):
    fake_bin("codex", "#!/bin/bash\necho 'usage limit reached' >&2\nexit 1\n")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    rc = main(["complete", "--client", "codex", "x"])
    err = capsys.readouterr().err
    assert rc == 1
    assert err.startswith("error: ") and not err.startswith("error: QuotaError")


def test_out_dir_is_file_typed_error(fake_bin, home, tmp_path, capsys):
    _stub(fake_bin, _WRITE)
    bad = tmp_path / "afile"
    bad.write_text("x")
    rc = main(["image", "--route", "codex", "-o", str(bad), "x"])
    cap = capsys.readouterr()
    assert rc == 1 and cap.out == ""
    assert cap.err.startswith("error: ClientError: ")
