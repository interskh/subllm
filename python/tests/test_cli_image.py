import io
import json
from pathlib import Path

import pytest

from subllm.cli import main
from subllm.errors import HumanActionRequired

from .test_chatgpt_web_ego import PNG_BYTES, URL, ev, fail, img_ok
from .test_chatgpt_web_llm import chrome, egoimg  # noqa: F401

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


def _web(out, *extra, prompt="a cube"):
    return main(["image", "--route", "web", "-o", str(out), *extra, prompt])


def test_web_success_json(egoimg, capsys):
    d, out = egoimg
    rc = _web(out)
    cap = capsys.readouterr()
    assert rc == 0 and cap.err == "" and cap.out.count("\n") == 1
    doc = json.loads(cap.out)
    assert set(doc) == {"paths", "route", "text", "conversation_url", "served_model"}
    assert doc["route"] == "web" and doc["conversation_url"] == URL
    assert len(doc["paths"]) == 1
    assert Path(doc["paths"][0]).parent == out.resolve()
    assert Path(doc["paths"][0]).read_bytes() == PNG_BYTES


def test_web_browser_flag_accepted(egoimg, capsys):
    d, out = egoimg
    assert _web(out, "--browser", "ego") == 0
    assert json.loads(capsys.readouterr().out)["route"] == "web"


def test_web_browser_flag_reaches_client(egoimg, chrome, capsys):
    d, out = egoimg
    assert _web(out, "--browser", "chrome") == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["text"] == "chrome" and chrome.calls[-1] == "run_image"
    assert not (d / "calls").exists()


def test_web_chrome_human_action_exits_3(egoimg, chrome, capsys):
    d, out = egoimg
    chrome.run_error = HumanActionRequired("log in", browser="chrome")
    assert _web(out, "--browser", "chrome") == 3
    cap = capsys.readouterr()
    assert cap.out == "" and cap.err.startswith("error: HumanActionRequired:")


def test_web_prompt_from_stdin(egoimg, capsys, monkeypatch):
    d, out = egoimg
    monkeypatch.setattr("sys.stdin", io.StringIO("stdin prompt"))
    assert main(["image", "--route", "web", "-o", str(out)]) == 0
    assert len(json.loads(capsys.readouterr().out)["paths"]) == 1
    assert "Generate an image: stdin prompt" in (d / "script.js").read_text()


def test_web_human_action_exits_3(egoimg, capsys, monkeypatch):
    d, out = egoimg
    monkeypatch.setenv("STUB_STDOUT", fail("human_action", "log in to chatgpt.com in ego lite"))
    monkeypatch.setenv("STUB_KEYPRESS", "0")
    monkeypatch.setenv("STUB_CODE", "1")
    assert _web(out) == 3
    cap = capsys.readouterr()
    assert cap.out == "" and cap.err.startswith("error: HumanActionRequired:")


def test_web_send_unconfirmed_includes_url(egoimg, capsys, monkeypatch):
    d, out = egoimg
    monkeypatch.setenv("STUB_STDOUT", ev(phase="accepted", conversation_url=URL))
    monkeypatch.setenv("STUB_CODE", "1")
    assert _web(out) == 1
    cap = capsys.readouterr()
    assert cap.out == ""
    assert cap.err.startswith("error: SendUnconfirmedError:") and f"(conversation: {URL})" in cap.err


def test_web_zero_images_is_output_error(egoimg, capsys, monkeypatch):
    d, out = egoimg
    monkeypatch.setenv("STUB_STDOUT", ev(phase="accepted", conversation_url=URL) + "\n" + fail("output", "no image in the answer", url=URL))
    monkeypatch.setenv("STUB_CODE", "1")
    assert _web(out) == 1
    cap = capsys.readouterr()
    assert cap.out == "" and cap.err.startswith("error: OutputError:")


def test_browser_rejected_for_codex_route(capsys):
    assert main(["image", "--route", "codex", "--browser", "ego", "-o", "o", "x"]) == 2
    assert "--browser" in capsys.readouterr().err


def test_web_write_failure_after_send_includes_url(egoimg, capsys, monkeypatch):
    d, out = egoimg
    monkeypatch.setattr("subllm._images.secrets.token_hex", lambda n: "deadbeef")
    monkeypatch.setattr("subllm._images.datetime", type("D", (), {
        "now": staticmethod(lambda tz=None: __import__("datetime").datetime(2026, 1, 2, 3, 4, 5))
    }))
    out.mkdir()
    (out / "20260102T030405-deadbeef-1.png").write_bytes(b"KEEP")
    assert _web(out) == 1
    cap = capsys.readouterr()
    assert cap.out == "" and cap.err.startswith("error: SendUnconfirmedError:")
    assert f"(conversation: {URL})" in cap.err
