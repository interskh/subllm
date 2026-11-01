import os
import re
from pathlib import Path

import pytest

from subllm import ClaudeLLM, CodexLLM, ImageGenerator, ImageResult
from subllm.errors import ClientError, OutputError, QuotaError, RegionError

PNG = b"\x89PNG\r\n\x1a\nfake-image-bytes"

_STUB = r'''#!/bin/bash
out=""; cdir=""
args=("$@")
while [ $# -gt 0 ]; do
  [ "$1" = "-o" ] && out="$2"
  [ "$1" = "-C" ] && cdir="$2"
  shift
done
echo run >> "{log}/calls.txt"
printf '%s\n' "${{args[@]}}" > "{log}/argv.txt"
echo "$CODEX_HOME" > "{log}/home.txt"
pwd -P > "{log}/pwd.txt"
echo "$cdir" > "{log}/cdir.txt"
ls -A . > "{log}/ls.txt"
cat "$CODEX_HOME/auth.json" > "{log}/auth.txt" 2>/dev/null
wc -c < /dev/stdin > "{log}/stdin.txt"
{body}
printf 'done' > "$out"
'''

_WRITE = r'''
mkdir -p "$CODEX_HOME/generated_images/t1" "$CODEX_HOME/plugins/x"
printf 'PNGDATA' > "$CODEX_HOME/generated_images/t1/exec-1.PNG"
printf 'PREVIEW' > "$CODEX_HOME/plugins/x/preview.png"
'''


@pytest.fixture
def env(fake_bin, tmp_path, monkeypatch):
    log = tmp_path / "log"
    log.mkdir()
    real = tmp_path / "realhome"
    real.mkdir()
    (real / "auth.json").write_text("ENV-AUTH")
    monkeypatch.setenv("CODEX_HOME", str(real))

    def make(body=_WRITE, tail=""):
        fake_bin("codex", _STUB.format(log=log, body=body) + tail)

    make()
    return log, tmp_path / "out", make


def test_success_copies_and_returns(env):
    log, out, _ = env
    res = CodexLLM(model="m").generate_image("a red cube", out)
    assert isinstance(res, ImageResult)
    assert res.route == "codex" and res.text == "done"
    assert res.conversation_url is None and res.served_model is None
    assert len(res.paths) == 1
    p = res.paths[0]
    assert p.is_absolute() and p.parent == out.resolve()
    assert re.fullmatch(r"\d{8}T\d{6}-[0-9a-f]{8}-1\.png", p.name)
    assert p.read_bytes() == b"PNGDATA"
    assert list(out.iterdir()) == [p]


def test_multiple_files_share_stem_and_sorted(env):
    log, out, make = env
    make(body=r'''
mkdir -p "$CODEX_HOME/generated_images/t1"
printf 'B' > "$CODEX_HOME/generated_images/t1/exec-2.png"
printf 'A' > "$CODEX_HOME/generated_images/t1/exec-1.png"
''')
    res = CodexLLM().generate_image("x", out)
    assert [p.read_bytes() for p in res.paths] == [b"A", b"B"]
    assert res.paths[0].name.rsplit("-", 1)[0] == res.paths[1].name.rsplit("-", 1)[0]
    assert res.paths[1].name.endswith("-2.png")


def test_plugin_previews_not_collected(env):
    _, out, _ = env
    res = CodexLLM().generate_image("x", out)
    assert [p.read_bytes() for p in res.paths] == [b"PNGDATA"]


def test_symlinks_not_collected(env):
    _, out, make = env
    make(body=r'''
mkdir -p "$CODEX_HOME/generated_images/t1"
printf 'SECRET' > "$CODEX_HOME/outside.png"
ln -s "$CODEX_HOME/outside.png" "$CODEX_HOME/generated_images/t1/link.png"
''')
    with pytest.raises(OutputError):
        CodexLLM().generate_image("x", out)


def test_no_image_is_output_error_single_attempt(env):
    log, out, make = env
    make(body="")
    with pytest.raises(OutputError):
        CodexLLM(attempts=3).generate_image("x", out)
    assert (log / "calls.txt").read_text().count("run") == 1
    assert not out.exists() or list(out.iterdir()) == []


def test_quota_error_writes_nothing(env):
    log, out, make = env
    make(body=_WRITE + 'echo "usage limit reached" >&2\nexit 1\n')
    with pytest.raises(QuotaError):
        CodexLLM().generate_image("x", out)
    assert not out.exists() or list(out.iterdir()) == []
    assert (log / "calls.txt").read_text().count("run") == 1


def test_fresh_scratch_home_even_with_codex_home(env, tmp_path):
    log, out, _ = env
    passed = tmp_path / "passed"
    (passed / "generated_images" / "old").mkdir(parents=True)
    (passed / "generated_images" / "old" / "exec-9.png").write_bytes(b"OLD")
    (passed / "auth.json").write_text("PASSED-AUTH")
    (passed / "AGENTS.md").write_text("persona")
    res = CodexLLM(codex_home=str(passed)).generate_image("x", out)
    home = Path((log / "home.txt").read_text().strip())
    assert home != passed and home != Path(os.environ["CODEX_HOME"])
    assert (log / "auth.txt").read_text() == "PASSED-AUTH"
    assert not home.exists()
    assert [p.read_bytes() for p in res.paths] == [b"PNGDATA"]
    assert (passed / "generated_images" / "old" / "exec-9.png").read_bytes() == b"OLD"


def test_auth_from_env_home_when_no_codex_home(env):
    log, out, _ = env
    CodexLLM().generate_image("x", out)
    assert (log / "auth.txt").read_text() == "ENV-AUTH"


def test_cwd_is_empty_work_dir_and_stdin_closed(env, tmp_path, monkeypatch):
    log, out, _ = env
    monkeypatch.chdir(tmp_path)
    import subprocess
    from subllm.drivers import codex_exec
    seen = {}
    real_run = subprocess.run

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(codex_exec.subprocess, "run", spy)
    CodexLLM().generate_image("x", out)
    assert seen["stdin"] is subprocess.DEVNULL
    cdir = (log / "cdir.txt").read_text().strip()
    assert cdir and Path((log / "pwd.txt").read_text().strip()) == Path(cdir).resolve()
    assert Path(cdir).resolve() != tmp_path.resolve()
    assert (log / "ls.txt").read_text() == ""
    assert (log / "stdin.txt").read_text().strip() == "0"
    assert not Path(cdir).exists()


def test_existing_files_untouched_and_out_dir_created(env, tmp_path):
    _, _, _ = env
    out = tmp_path / "a" / "b"
    out.mkdir(parents=True)
    (out / "keep.png").write_bytes(b"KEEP")
    res = CodexLLM().generate_image("x", out)
    assert (out / "keep.png").read_bytes() == b"KEEP"
    assert res.paths[0].name != "keep.png"
    fresh = tmp_path / "new" / "deep"
    assert CodexLLM().generate_image("x", fresh).paths[0].parent == fresh.resolve()


def test_exclusive_create_never_overwrites(env, monkeypatch):
    _, out, _ = env
    monkeypatch.setattr("subllm._images.secrets.token_hex", lambda n: "deadbeef")
    monkeypatch.setattr("subllm._images.datetime", type("D", (), {
        "now": staticmethod(lambda tz=None: __import__("datetime").datetime(2026, 1, 2, 3, 4, 5))
    }))
    out.mkdir()
    (out / "20260102T030405-deadbeef-1.png").write_bytes(b"KEEP")
    with pytest.raises(ClientError):
        CodexLLM().generate_image("x", out)
    assert (out / "20260102T030405-deadbeef-1.png").read_bytes() == b"KEEP"


def test_argv_has_instruction_prompt_and_isolation_flags(env):
    log, out, _ = env
    CodexLLM(model="gpt-5.4", reasoning_effort="high", search=True).generate_image(
        "a red cube on a table", out)
    argv = (log / "argv.txt").read_text().splitlines()
    assert argv[argv.index("-s") + 1] == "read-only"
    for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral"):
        assert flag in argv
    assert "project_doc_max_bytes=0" in argv
    assert 'web_search="live"' not in argv
    assert argv[argv.index("-m") + 1] == "gpt-5.4"
    msg = "\n".join(argv[argv.index("--") + 1:])
    assert "image generation tool exactly once" in msg
    assert "SVG" in msg and "one word" in msg
    assert msg.endswith("a red cube on a table")


def test_region_guard_runs_first(env):
    log, out, _ = env

    class Guard:
        def check(self):
            raise RegionError("blocked")

    with pytest.raises(RegionError):
        CodexLLM(region_guard=Guard()).generate_image("x", out)
    assert not (log / "calls.txt").exists()


def test_image_generator_membership():
    assert isinstance(CodexLLM(), ImageGenerator)
    assert not isinstance(ClaudeLLM(), ImageGenerator)


def test_out_dir_is_file_fails_before_codex(env, tmp_path):
    log, _, _ = env
    bad = tmp_path / "afile"
    bad.write_text("x")
    with pytest.raises(ClientError, match="afile"):
        CodexLLM().generate_image("x", bad)
    assert not (log / "calls.txt").exists()


def test_symlinked_generated_images_dir_not_collected(env, tmp_path):
    log, out, make = env
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.png").write_bytes(b"SECRET")
    make(body=f'ln -s "{outside}" "$CODEX_HOME/generated_images"\n')
    with pytest.raises(OutputError):
        CodexLLM().generate_image("x", out)
    assert list(out.iterdir()) == []


def test_auth_copy_failure_removes_scratch(env, monkeypatch, tmp_path):
    import tempfile
    from subllm.drivers import codex_exec
    log, out, _ = env
    made = []
    real_mkdtemp = tempfile.mkdtemp

    def spy(*a, **k):
        d = real_mkdtemp(*a, **k)
        if k.get("prefix") == "subllm-codex-":
            made.append(d)
        return d

    def boom(*a, **k):
        raise PermissionError("nope")

    monkeypatch.setattr(codex_exec.tempfile, "mkdtemp", spy)
    monkeypatch.setattr(codex_exec.shutil, "copy2", boom)
    with pytest.raises(PermissionError):
        CodexLLM().generate_image("x", out)
    assert len(made) == 1 and not Path(made[0]).exists()
    assert not (log / "calls.txt").exists()


def test_out_dir_symlink_loop_is_client_error(env, tmp_path):
    from subllm.errors import ClientError
    log, _, _ = env
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.symlink_to(b)
    b.symlink_to(a)
    with pytest.raises(ClientError, match="out_dir"):
        CodexLLM().generate_image("x", a / "sub")
    assert not (log / "calls.txt").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permissions")
def test_out_dir_without_search_permission_is_client_error(env, tmp_path):
    from subllm.errors import ClientError
    log, _, _ = env
    d = tmp_path / "wo"
    d.mkdir()
    d.chmod(0o200)
    try:
        with pytest.raises(ClientError):
            CodexLLM().generate_image("x", d)
    finally:
        d.chmod(0o700)
    assert not (log / "calls.txt").exists()
