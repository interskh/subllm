import base64
import fcntl
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from subllm.drivers.chatgpt_web.ego import EgoDriver, build_script, lock_path, parse_output
from subllm.errors import (
    ClientError,
    HumanActionRequired,
    OutputError,
    QuotaError,
    SendUnconfirmedError,
)

URL = "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555"


def ev(**o):
    return "SUBLLM_EVENT " + json.dumps(o)


def res(**o):
    return "SUBLLM_RESULT " + json.dumps(o)


OK = res(ok=True, text="hello", format="markdown", images=[], served_model="gpt-x", conversation_url=URL)


def fail(kind, detail="d", url=None):
    return res(ok=False, kind=kind, detail=detail, conversation_url=url, text=None, served_model=None)


STUB = r"""#!/bin/bash
case "$1" in
  --version) echo "ego-browser 1.0"; exit 0 ;;
  skill) echo "[ego-browser:skill] ego-browser usage guide ${STUB_GUIDE:-v2.2.0} (macos)"; exit 0 ;;
esac
tee "$STUB_DIR/script.js" > /dev/null
if grep -q SUBLLM_HEALTH_ "$STUB_DIR/script.js"; then
  [ "$STUB_HEALTH" = "bad" ] && exit 0
  tok=$(grep -o 'SUBLLM_HEALTH_[0-9a-f]*' "$STUB_DIR/script.js" | head -1)
  echo "$tok"; exit 0
fi
touch "$STUB_DIR/ran"
kp=$(sed -n 's/^const KEYPRESS_FILE = "\(.*\)";$/\1/p' "$STUB_DIR/script.js")
[ "$STUB_KEYPRESS" = "1" ] && echo keypress > "$kp"
[ -n "$STUB_STDOUT" ] && printf '%s\n' "$STUB_STDOUT"
[ -n "$STUB_STDERR" ] && printf '%s\n' "$STUB_STDERR" >&2
exit "${STUB_CODE:-0}"
"""


@pytest.fixture
def ego(fake_bin, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / "stub"
    d.mkdir()
    monkeypatch.setenv("STUB_DIR", str(d))
    fake_bin("ego-browser", STUB)

    def scenario(stderr="", *, keypress=False, code=0, stdout=""):
        monkeypatch.setenv("STUB_STDERR", stderr)
        monkeypatch.setenv("STUB_STDOUT", stdout)
        monkeypatch.setenv("STUB_KEYPRESS", "1" if keypress else "0")
        monkeypatch.setenv("STUB_CODE", str(code))

    scenario.dir = d
    return scenario


def run(prompt="hi", timeout_s=5):
    return EgoDriver().run_text(prompt, timeout_s=timeout_s)


def lines(*ls):
    return "\n".join(ls)


def test_success(ego):
    ego(lines("[ego-browser:notice] x", ev(phase="keypress"), ev(phase="accepted", conversation_url=URL), OK), keypress=True)
    r = run()
    assert (r.text, r.format, r.served_model, r.conversation_url) == ("hello", "markdown", "gpt-x", URL)


def test_result_on_stdout_only(ego):
    ego(keypress=True, stdout=lines(ev(phase="accepted", conversation_url=URL), OK))
    assert run().text == "hello"


def test_logged_out(ego):
    ego(fail("human_action", "log in to chatgpt.com in ego lite"))
    with pytest.raises(HumanActionRequired) as ei:
        run()
    assert ei.value.browser == "ego" and "log in" in ei.value.detail


def test_challenge(ego):
    ego(fail("human_action", "Cloudflare challenge"), code=1)
    with pytest.raises(HumanActionRequired) as ei:
        run()
    assert ei.value.detail == "Cloudflare challenge"


def test_quota_before_keypress(ego):
    ego(fail("quota"), code=1)
    with pytest.raises(QuotaError):
        run()


def test_work_mode(ego):
    ego(fail("client", "composer is in Work mode"), code=1)
    with pytest.raises(ClientError, match="Work mode") as ei:
        run()
    assert type(ei.value) is ClientError


def test_rejected_after_keypress(ego):
    ego(lines(ev(phase="keypress"), ev(phase="rejected"), fail("client", "send rejected")), keypress=True, code=1)
    with pytest.raises(ClientError, match="not resent") as ei:
        run()
    assert type(ei.value) is ClientError


def test_client_failure_after_keypress_without_rejected_is_unconfirmed(ego):
    ego(fail("client", "send not confirmed"), keypress=True, code=1)
    with pytest.raises(SendUnconfirmedError):
        run()


def test_quota_dialog_after_keypress(ego):
    ego(lines(ev(phase="accepted", conversation_url=URL), fail("quota", "limit", url=None)), keypress=True, code=1)
    with pytest.raises(SendUnconfirmedError) as ei:
        run()
    assert not isinstance(ei.value, QuotaError)
    assert ei.value.conversation_url == URL


def test_output_error_after_keypress(ego):
    ego(fail("output", "empty answer", url=URL), keypress=True, code=1)
    with pytest.raises(OutputError):
        run()


def test_crash_after_keypress_no_result(ego):
    ego(ev(phase="accepted", conversation_url=URL), keypress=True, code=1)
    with pytest.raises(SendUnconfirmedError) as ei:
        run()
    assert ei.value.conversation_url == URL


def test_crash_before_keypress_has_stderr_tail(ego):
    ego("boom: runtime died", code=1)
    with pytest.raises(ClientError, match="boom: runtime died") as ei:
        run()
    assert type(ei.value) is ClientError


def test_ok_with_nonzero_exit_is_unconfirmed(ego):
    ego(OK, keypress=True, code=1)
    with pytest.raises(SendUnconfirmedError):
        run()


def test_ok_without_keypress_record_is_client_error(ego):
    ego(OK)
    with pytest.raises(ClientError, match="no send was recorded") as ei:
        run()
    assert type(ei.value) is ClientError


def test_two_results_are_missing(ego):
    ego(lines(OK, OK), keypress=True)
    with pytest.raises(SendUnconfirmedError):
        run()


def test_timeout_after_keypress_is_unconfirmed(ego, monkeypatch):
    import subllm.drivers.chatgpt_web.ego as mod

    real = mod.subprocess.run

    def fake(cmd, **kw):
        if cmd[1:] == ["nodejs"] and "SUBLLM_HEALTH_" not in kw["input"]:
            kp = re.search(r'^const KEYPRESS_FILE = "(.*)";$', kw["input"], re.M).group(1)
            open(kp, "w").write("keypress")
            raise mod.subprocess.TimeoutExpired(cmd, 1, output=ev(phase="accepted", conversation_url=URL).encode())
        return real(cmd, **kw)

    monkeypatch.setattr(mod.subprocess, "run", fake)
    ego()
    with pytest.raises(SendUnconfirmedError) as ei:
        run()
    assert ei.value.conversation_url == URL


def test_keypress_dir_removed(ego):
    ego(OK, keypress=True)
    run()
    script = (ego.dir / "script.js").read_text()
    kp = re.search(r'^const KEYPRESS_FILE = "(.*)";$', script, re.M).group(1)
    assert not os.path.exists(os.path.dirname(kp))


def test_available_ok(ego):
    assert EgoDriver().available() == (True, "ok")


def test_ego_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    ok, reason = EgoDriver().available()
    assert not ok and "not on PATH" in reason


def test_health_token_missing(ego, monkeypatch):
    monkeypatch.setenv("STUB_HEALTH", "bad")
    ok, reason = EgoDriver().available()
    assert not ok and "health" in reason


def test_guide_v3(ego, monkeypatch):
    monkeypatch.setenv("STUB_GUIDE", "v3.0.1")
    d = EgoDriver()
    assert d.available() == (True, "ok")
    with pytest.raises(ClientError, match=r"v3\.0\.1.*2\.x"):
        d.check_guide()


def test_crlf_and_tab_prompt_normalized(ego):
    ego(OK, keypress=True)
    run("a\r\nb\rc\td")
    s = (ego.dir / "script.js").read_text()
    line = next(l for l in s.splitlines() if l.startswith("const ARGS = "))
    assert json.loads(line[len("const ARGS = "):].rstrip(";"))["prompt"] == "a\nb\nc    d"


def test_early_chat_with_record_is_unconfirmed(ego):
    ego(fail("client", "chat opened before the send key"), keypress=True, code=1)
    with pytest.raises(SendUnconfirmedError):
        run()


def test_record_is_written_once_before_the_first_typed_key():
    body = script()
    assert body.count("record();") == 1
    assert body.index("record();") < body.index("keyboard.type")
    assert body.index("record();") < body.index('keyboard.press("Enter")')


def test_composer_mismatch_with_record_is_unconfirmed(ego):
    ego(fail("client", "composer text does not match the prompt"), keypress=True, code=1)
    with pytest.raises(SendUnconfirmedError):
        run()


def test_empty_prompt_is_client_error_before_any_subprocess(ego):
    for prompt in ("", " \t\r\n "):
        with pytest.raises(ClientError, match="empty"):
            run(prompt)
    assert not (ego.dir / "ran").exists() and not (ego.dir / "script.js").exists()


def test_answer_deadline_starts_after_enter():
    body = script()
    enter = body.index('keyboard.press("Enter")')
    assert body.index("answerDeadline = Date.now() + ARGS.timeout_ms", enter) > enter
    assert "DEADLINE = START" not in body


def test_subprocess_budget(ego, monkeypatch):
    import subllm.drivers.chatgpt_web.ego as mod

    ego(OK, keypress=True)
    real = mod.subprocess.run
    seen = {}

    def spy(cmd, **kw):
        if cmd[1:] == ["nodejs"]:
            seen["t"] = kw["timeout"]
        return real(cmd, **kw)

    monkeypatch.setattr(mod.subprocess, "run", spy)
    run("x" * 100, timeout_s=7)
    assert seen["t"] == 7 + 120 + 6


def test_legacy_composer_selector():
    assert "#prompt-textarea" in script()


def test_lock_open_oserror_is_client_error(ego, tmp_path, monkeypatch):
    (tmp_path / ".local").write_text("not a dir")
    with pytest.raises(ClientError, match="lock"):
        run()


def test_held_lock_blocks_before_running(ego, tmp_path):
    ego(OK, keypress=True)
    p = lock_path()
    p.parent.mkdir(parents=True)
    with open(p, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        with pytest.raises(ClientError, match="another chatgpt-web call"):
            run(timeout_s=0.3)
    assert not (ego.dir / "ran").exists()


def test_lock_under_home_and_released(ego, tmp_path):
    ego(OK, keypress=True)
    run()
    assert lock_path() == tmp_path / ".local/share/subllm/chatgpt-web.lock"
    assert lock_path().exists()
    with open(lock_path(), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)


def script(prompt="p"):
    return build_script(prompt, space="subllm-chatgpt-abcd1234", timeout_ms=1000, keypress_file="/x/keypress")


def test_prompt_is_json_literal():
    prompt = 'say "hi"\n\\back </script>   end\n\nlast'
    line = next(l for l in script(prompt).splitlines() if l.startswith("const ARGS = "))
    args = json.loads(line[len("const ARGS = "):].rstrip(";"))
    assert args["prompt"] == prompt
    assert "\n" not in line


def test_script_markers():
    s = script()
    assert 'const KEYPRESS_FILE = "/x/keypress";' in s.splitlines()
    for needle in ["finish({ keep: [] })", "handOff()", "Shift+Enter", "delay: 40", "bringToFront"]:
        assert needle in s
    body = s.split("\n", 3)[3]
    for bad in ["insertText", ".paste(", ".fill(", "backend-api"]:
        assert bad not in body


def test_space_name_differs_per_call(ego):
    ego(OK, keypress=True)
    names = []
    for _ in range(2):
        run()
        s = (ego.dir / "script.js").read_text()
        names.append(re.search(r'"space": "([^"]*)"', s).group(1))
    assert all(re.fullmatch(r"subllm-chatgpt-[0-9a-f]{8}", n) for n in names)
    assert names[0] != names[1]


_FAKE_RUNTIME = r"""
const __typed = { done: false };
const __page = {
  url: async () => (__typed.done ? "https://chatgpt.com/c/fake-id" : "https://chatgpt.com/"),
  goto: async () => {},
  click: async () => {},
  cdp: async () => {},
  keyboard: {
    type: async () => { __typed.done = true; },
    press: async () => { console.log("PRESSED_ENTER"); },
  },
  evaluate: async (expr) => {
    if (expr.startsWith("!!document")) return true;
    const call = expr.slice(-80);
    if (call.includes(").state(")) return { logged_in: true, login_prompt: false, mode_surface: "chat", busy: false, quota: false };
    if (call.includes(").composerMatches(")) return false;
    throw new Error("unexpected evaluate: " + call);
  },
};
const taskSpace = async () => ({ page: () => __page, finish: async () => {}, handOff: async () => {} });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_early_send_during_typing_writes_record_and_reports_chat_opened(tmp_path):
    kp = tmp_path / "keypress"
    body = build_script("hi", space="subllm-chatgpt-abcd1234", timeout_ms=1000, keypress_file=str(kp))
    prog = tmp_path / "run.js"
    prog.write_text(_FAKE_RUNTIME + "(async () => {\n" + body + "\n})();\n")
    p = subprocess.run(["node", str(prog)], capture_output=True, text=True, timeout=60)
    events, results = parse_output(p.stdout)
    assert kp.exists()
    assert {"phase": "keypress"} in events
    assert "PRESSED_ENTER" not in p.stdout
    assert len(results) == 1
    assert results[0]["ok"] is False and results[0]["kind"] == "client"
    assert "chat opened before the send key" in results[0]["detail"]


def _run_fake(tmp_path, goto_js):
    kp = tmp_path / "keypress"
    body = build_script("hi", space="subllm-chatgpt-abcd1234", timeout_ms=1000, keypress_file=str(kp))
    runtime = _FAKE_RUNTIME.replace("  goto: async () => {},", goto_js)
    prog = tmp_path / "run.js"
    prog.write_text(runtime + "(async () => {\n" + body + "\n})();\n")
    p = subprocess.run(["node", str(prog)], capture_output=True, text=True, timeout=60)
    return kp, p.stdout, parse_output(p.stdout)[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_goto_load_timeout_after_commit_continues_to_state_loop(tmp_path):
    goto = (
        "  goto: async (u, o) => { console.log('GOTO_OPTS ' + JSON.stringify(o));"
        " throw new Error('page.goto timed out after 15000ms waiting for load;"
        ' navigation committed at "https://chatgpt.com/"\'); },'
    )
    kp, out, results = _run_fake(tmp_path, goto)
    assert 'GOTO_OPTS {"waitUntil":"domcontentloaded"}' in out
    assert len(results) == 1 and "chat opened before the send key" in results[0]["detail"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_goto_failure_before_commit_is_client_without_record(tmp_path):
    goto = "  goto: async () => { throw new Error('EGO_PAGE_UNRESPONSIVE'); },"
    kp, out, results = _run_fake(tmp_path, goto)
    assert not kp.exists()
    assert len(results) == 1 and results[0]["kind"] == "client"
    assert "EGO_PAGE_UNRESPONSIVE" in results[0]["detail"]


def _run_variant(tmp_path, *swaps):
    kp = tmp_path / "keypress"
    body = build_script("hi", space="subllm-chatgpt-abcd1234", timeout_ms=1000, keypress_file=str(kp))
    runtime = _FAKE_RUNTIME
    for old, new in swaps:
        assert old in runtime, old
        runtime = runtime.replace(old, new)
    prog = tmp_path / "run.js"
    prog.write_text(runtime + "(async () => {\n" + body + "\n})();\n")
    p = subprocess.run(["node", str(prog)], capture_output=True, text=True, timeout=60)
    return kp, p.stdout, parse_output(p.stdout)[1]


_TYPE_OK = "    type: async () => { __typed.done = true; },"
_FAST_CLOCK = (
    "const __typed",
    "let __t = Date.now(); Date.now = () => (__t += 1000);\n"
    "const __st = setTimeout; globalThis.setTimeout = (f) => __st(f, 0);\nconst __typed",
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_typing_rpc_throwing_after_an_early_send_still_leaves_the_record(tmp_path):
    kp, out, results = _run_variant(
        tmp_path, (_TYPE_OK, "    type: async () => { __typed.done = true; throw new Error('RPC_LOST'); },")
    )
    assert kp.exists() and "PRESSED_ENTER" not in out
    assert len(results) == 1 and results[0]["kind"] == "client" and "RPC_LOST" in results[0]["detail"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_composer_mismatch_leaves_record_and_never_presses_enter(tmp_path):
    kp, out, results = _run_variant(tmp_path, (_TYPE_OK, "    type: async () => {},"))
    assert kp.exists() and "PRESSED_ENTER" not in out
    assert len(results) == 1 and "composer text does not match" in results[0]["detail"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_never_loaded_page_is_client_error_not_a_login_wall(tmp_path):
    kp, out, results = _run_variant(
        tmp_path,
        _FAST_CLOCK,
        ("logged_in: true, login_prompt: false", "logged_in: false, login_prompt: false"),
        ("handOff: async () => {}", "handOff: async () => { console.log('HANDOFF'); }"),
    )
    assert not kp.exists() and "HANDOFF" not in out
    assert len(results) == 1 and results[0]["kind"] == "client"
    assert "did not become ready" in results[0]["detail"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_login_prompt_is_a_human_action_with_hand_off(tmp_path):
    kp, out, results = _run_variant(
        tmp_path,
        _FAST_CLOCK,
        ("logged_in: true, login_prompt: false", "logged_in: false, login_prompt: true"),
        ("handOff: async () => {}", "handOff: async () => { console.log('HANDOFF'); }"),
    )
    assert not kp.exists() and "HANDOFF" in out
    assert results[0]["kind"] == "human_action"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_login_control_before_session_hydrates_is_not_a_hand_off(tmp_path):
    kp, out, results = _run_variant(
        tmp_path,
        ("const __typed", "let __polls = 0;\nconst __typed"),
        (
            "return { logged_in: true, login_prompt: false,",
            "return __polls++ < 2 ? { logged_in: false, login_prompt: true } : { logged_in: true, login_prompt: false,",
        ),
        ("handOff: async () => {}", "handOff: async () => { console.log('HANDOFF'); }"),
    )
    assert "HANDOFF" not in out
    assert len(results) == 1 and "chat opened before the send key" in results[0]["detail"]


def test_fcntl_is_not_imported_at_module_level():
    import ast
    import subllm.drivers.chatgpt_web.ego as mod

    tree = ast.parse(open(mod.__file__).read())
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not any(a.name == "fcntl" for n in top if isinstance(n, ast.Import) for a in n.names)


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)
WEBP_BYTES = b"RIFF\x1a\x00\x00\x00WEBPVP8L\x0d\x00\x00\x00/\x00\x00\x00\x10\x07\x10\x11\x11\x88\x88\xfe\x07\x00"

IMG_STUB = STUB.replace(
    "[ -n \"$STUB_STDOUT\" ] && printf '%s\\n' \"$STUB_STDOUT\"",
    'idir=$(sed -n \'s/^const ARGS = .*"image_dir": "\\([^"]*\\)".*$/\\1/p\' "$STUB_DIR/script.js")\n'
    'echo "$idir" > "$STUB_DIR/idir"\n'
    '[ -n "$STUB_IMG_CMD" ] && eval "$STUB_IMG_CMD"\n'
    "[ -n \"$STUB_STDOUT\" ] && printf '%s\\n' \"${STUB_STDOUT//@IDIR@/$idir}\"",
)
assert IMG_STUB != STUB


def img_ok(*names, text="here"):
    return res(
        ok=True, text=text, format="plain", images=[f"@IDIR@/{n}" for n in names],
        served_model="gpt-x", conversation_url=URL,
    )


@pytest.fixture
def egoimg(fake_bin, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / "stub"
    d.mkdir()
    (d / "png.bin").write_bytes(PNG_BYTES)
    (d / "webp.bin").write_bytes(WEBP_BYTES)
    (d / "txt.bin").write_bytes(b"not an image")
    monkeypatch.setenv("STUB_DIR", str(d))
    fake_bin("ego-browser", IMG_STUB)
    out = tmp_path / "out"

    def scenario(stdout, cmd="", *, keypress=True, code=0):
        monkeypatch.setenv("STUB_STDERR", "")
        monkeypatch.setenv("STUB_STDOUT", stdout)
        monkeypatch.setenv("STUB_IMG_CMD", cmd)
        monkeypatch.setenv("STUB_KEYPRESS", "1" if keypress else "0")
        monkeypatch.setenv("STUB_CODE", str(code))

    scenario.dir = d
    scenario.out = out
    out.mkdir()
    return scenario


def run_image(out, prompt="draw", timeout_s=5):
    return EgoDriver().run_image(prompt, out, timeout_s=timeout_s)


def _stub_idir(egoimg):
    return Path((egoimg.dir / "idir").read_text().strip())


def test_image_one_png(egoimg):
    egoimg(img_ok("img-1", text="done"), 'cp "$STUB_DIR/png.bin" "$idir/img-1"')
    r = run_image(egoimg.out)
    assert r.route == "web" and r.text == "done" and r.conversation_url == URL and r.served_model == "gpt-x"
    assert len(r.paths) == 1 and r.paths[0].parent == egoimg.out.resolve()
    assert re.fullmatch(r"\d{8}T\d{6}-[0-9a-f]{8}-1\.png", r.paths[0].name)
    assert r.paths[0].read_bytes() == PNG_BYTES


def test_image_two_files_keep_order_and_extensions(egoimg):
    egoimg(
        img_ok("img-1", "img-2"),
        'cp "$STUB_DIR/png.bin" "$idir/img-1"; cp "$STUB_DIR/webp.bin" "$idir/img-2"',
    )
    r = run_image(egoimg.out)
    assert [p.name.split("-", 2)[2] for p in r.paths] == ["1.png", "2.webp"]
    assert r.paths[1].read_bytes() == WEBP_BYTES


def test_image_jpeg_extension(egoimg):
    egoimg(img_ok("img-1"), 'printf "\\xff\\xd8\\xff\\xe0JFIF" > "$idir/img-1"')
    assert run_image(egoimg.out).paths[0].suffix == ".jpg"


def test_image_script_carries_kind_and_dir(egoimg):
    egoimg(img_ok("img-1"), 'cp "$STUB_DIR/png.bin" "$idir/img-1"')
    run_image(egoimg.out)
    line = next(l for l in (egoimg.dir / "script.js").read_text().splitlines() if l.startswith("const ARGS = "))
    args = json.loads(line[len("const ARGS = "):].rstrip(";"))
    assert args["kind"] == "image" and args["image_dir"] == str(_stub_idir(egoimg))


def test_image_output_kind_is_output_error(egoimg):
    egoimg(fail("output", "no image in the answer", url=URL), code=1)
    with pytest.raises(OutputError, match="no image"):
        run_image(egoimg.out)
    assert list(egoimg.out.iterdir()) == []


def test_image_listed_file_missing_is_unconfirmed(egoimg):
    egoimg(img_ok("img-1"))
    with pytest.raises(SendUnconfirmedError) as ei:
        run_image(egoimg.out)
    assert ei.value.conversation_url == URL
    assert list(egoimg.out.iterdir()) == []


def test_image_empty_list_on_ok_is_unconfirmed(egoimg):
    egoimg(img_ok())
    with pytest.raises(SendUnconfirmedError) as ei:
        run_image(egoimg.out)
    assert ei.value.conversation_url == URL


def test_image_path_outside_tmpdir_is_unconfirmed(egoimg, tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG_BYTES)
    egoimg(res(ok=True, text="", format="plain", images=[str(outside)], served_model=None, conversation_url=URL))
    with pytest.raises(SendUnconfirmedError) as ei:
        run_image(egoimg.out)
    assert ei.value.conversation_url == URL
    assert list(egoimg.out.iterdir()) == []


def test_image_nested_path_is_unconfirmed(egoimg):
    egoimg(img_ok("sub/img-1"), 'mkdir "$idir/sub"; cp "$STUB_DIR/png.bin" "$idir/sub/img-1"')
    with pytest.raises(SendUnconfirmedError):
        run_image(egoimg.out)


def test_image_symlink_is_unconfirmed(egoimg, tmp_path):
    target = tmp_path / "real.png"
    target.write_bytes(PNG_BYTES)
    egoimg(img_ok("img-1"), f'ln -s {target} "$idir/img-1"')
    with pytest.raises(SendUnconfirmedError):
        run_image(egoimg.out)
    assert list(egoimg.out.iterdir()) == []


def test_image_non_image_bytes_is_unconfirmed(egoimg):
    egoimg(img_ok("img-1"), 'cp "$STUB_DIR/txt.bin" "$idir/img-1"')
    with pytest.raises(SendUnconfirmedError) as ei:
        run_image(egoimg.out)
    assert ei.value.conversation_url == URL


def test_image_one_bad_file_saves_nothing(egoimg):
    egoimg(img_ok("img-1", "img-2"), 'cp "$STUB_DIR/png.bin" "$idir/img-1"; cp "$STUB_DIR/txt.bin" "$idir/img-2"')
    with pytest.raises(SendUnconfirmedError):
        run_image(egoimg.out)
    assert list(egoimg.out.iterdir()) == []


def test_image_existing_target_is_never_overwritten(egoimg, monkeypatch):
    monkeypatch.setattr("subllm._images.secrets.token_hex", lambda n: "deadbeef")
    monkeypatch.setattr("subllm._images.datetime", type("D", (), {
        "now": staticmethod(lambda tz=None: __import__("datetime").datetime(2026, 1, 2, 3, 4, 5))
    }))
    keep = egoimg.out / "20260102T030405-deadbeef-1.png"
    keep.write_bytes(b"KEEP")
    egoimg(img_ok("img-1"), 'cp "$STUB_DIR/png.bin" "$idir/img-1"')
    with pytest.raises(SendUnconfirmedError, match="cannot write image") as ei:
        run_image(egoimg.out)
    assert ei.value.conversation_url == URL
    assert keep.read_bytes() == b"KEEP"


def test_image_tmpdir_removed(egoimg):
    egoimg(img_ok("img-1"), 'cp "$STUB_DIR/png.bin" "$idir/img-1"')
    run_image(egoimg.out)
    assert not _stub_idir(egoimg).exists()


def test_image_tmpdir_removed_on_rejection(egoimg):
    egoimg(img_ok("img-1"), 'cp "$STUB_DIR/txt.bin" "$idir/img-1"')
    with pytest.raises(SendUnconfirmedError):
        run_image(egoimg.out)
    assert not _stub_idir(egoimg).exists()


def test_image_subprocess_budget_adds_fetch_time(egoimg, monkeypatch):
    import subllm.drivers.chatgpt_web.ego as mod

    egoimg(img_ok("img-1"), 'cp "$STUB_DIR/png.bin" "$idir/img-1"')
    real = mod.subprocess.run
    seen = {}

    def spy(cmd, **kw):
        if cmd[1:] == ["nodejs"]:
            seen["t"] = kw["timeout"]
        return real(cmd, **kw)

    monkeypatch.setattr(mod.subprocess, "run", spy)
    run_image(egoimg.out, "x" * 100, timeout_s=7)
    assert seen["t"] == 7 + 120 + 6 + 60


def test_image_text_run_script_is_text_kind(ego):
    ego(OK, keypress=True)
    run()
    line = next(l for l in (ego.dir / "script.js").read_text().splitlines() if l.startswith("const ARGS = "))
    assert json.loads(line[len("const ARGS = "):].rstrip(";"))["kind"] == "text"


_IMG_RUNTIME_SWAPS = [
    ("    if (call.includes(\").composerMatches(\")) return false;",
     "    if (call.includes(\").composerMatches(\")) return true;\n"
     "    if (call.includes(\").acceptance(\")) return { state: 'accepted', turn_key: 'k', conversation_url: 'https://chatgpt.com/c/fake-id' };\n"
     "    if (call.includes(\").turnStatus(\")) return __script.length > 1 ? __script.shift() : __script[0];\n"
     "    if (call.includes(\").captureText(\")) return { text: 'cap', format: 'plain' };\n"
     "    if (call.includes(\").servedModel(\")) return 'm1';"),
    (_TYPE_OK, "    type: async () => {},"),
    ('press: async () => { console.log("PRESSED_ENTER"); },', 'press: async () => { __typed.done = true; },'),
    ("  goto: async () => {},", "  goto: async () => {},\n  fetch: async (src, o) => { __fetches.push([src, o]); return __fetchResult(src); },"),
]


def _ts(images=(), stop=False, done=None, text="t"):
    done = (not stop) if done is None else done
    return {"done": done and text != "", "text": text, "stop_visible": stop, "images": list(images), "error_dialog": None}


def _run_image_loop(tmp_path, seq, fetch_result="({ ok: true, status: 200 })", timeout_ms=600000):
    kp = tmp_path / "keypress"
    body = build_script(
        "hi", space="subllm-chatgpt-abcd1234", timeout_ms=timeout_ms, keypress_file=str(kp),
        kind="image", image_dir="/img/dir",
    )
    runtime = _FAKE_RUNTIME.replace(*_FAST_CLOCK)
    for old, new in _IMG_RUNTIME_SWAPS:
        assert old in runtime, old
        runtime = runtime.replace(old, new)
    runtime = runtime.replace(
        "const __typed",
        f"const __script = {json.dumps(seq)};\nconst __fetches = [];\nconst __fetchResult = (src) => {fetch_result};\n"
        "process.on('exit', () => console.log('FETCHES ' + JSON.stringify(__fetches)));\nconst __typed",
        1,
    )
    prog = tmp_path / "run.js"
    prog.write_text(runtime + "(async () => {\n" + body + "\n})();\n")
    p = subprocess.run(["node", str(prog)], capture_output=True, text=True, timeout=60)
    fetches = json.loads(re.search(r"^FETCHES (.*)$", p.stdout, re.M).group(1))
    return fetches, parse_output(p.stdout)[1]


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
A, B = "blob:a", "blob:b"


@needs_node
def test_loop_images_stabilise_then_fetch(tmp_path):
    fetches, results = _run_image_loop(tmp_path, [_ts([A]), _ts([A, B]), _ts([A, B])])
    assert fetches == [
        [A, {"saveAs": "/img/dir/img-1", "timeout": 30000}],
        [B, {"saveAs": "/img/dir/img-2", "timeout": 30000}],
    ]
    assert results == [{
        "ok": True, "text": "cap", "format": "plain", "images": ["/img/dir/img-1", "/img/dir/img-2"],
        "served_model": "m1", "conversation_url": "https://chatgpt.com/c/fake-id",
    }]


@needs_node
def test_loop_waits_while_stop_is_visible(tmp_path):
    seq = [_ts([A], stop=True)] * 4 + [_ts([A])]
    fetches, results = _run_image_loop(tmp_path, seq)
    assert len(fetches) == 1 and results[0]["ok"] is True


@needs_node
def test_loop_stop_never_clears_times_out_without_fetch(tmp_path):
    fetches, results = _run_image_loop(tmp_path, [_ts([A], stop=True)], timeout_ms=20000)
    assert fetches == [] and results[0]["kind"] == "client" and "timed out" in results[0]["detail"]


@needs_node
def test_loop_text_only_answer_is_output_after_grace(tmp_path):
    fetches, results = _run_image_loop(tmp_path, [_ts([], text="I cannot")])
    assert fetches == []
    assert results[0]["ok"] is False and results[0]["kind"] == "output" and "no image" in results[0]["detail"]


@needs_node
def test_loop_grace_resets_when_images_appear(tmp_path):
    seq = [_ts([], text="x")] * 3 + [_ts([A])] * 2
    fetches, results = _run_image_loop(tmp_path, seq)
    assert len(fetches) == 1 and results[0]["ok"] is True


@needs_node
def test_loop_grace_resets_when_stop_reappears(tmp_path):
    seq = [_ts([], text="x")] * 4 + [_ts([], stop=True)] + [_ts([], text="x")] * 4 + [_ts([A])] * 2
    fetches, results = _run_image_loop(tmp_path, seq)
    assert len(fetches) == 1 and results[0]["ok"] is True


@needs_node
def test_loop_fetch_failure_is_client_after_keypress(tmp_path):
    fetches, results = _run_image_loop(tmp_path, [_ts([A])] * 2, fetch_result="({ ok: false, status: 403 })")
    assert (tmp_path / "keypress").exists() and len(fetches) == 1
    assert results[0]["ok"] is False and results[0]["kind"] == "client" and "image fetch failed" in results[0]["detail"]


@needs_node
def test_loop_empty_caption_text_is_allowed_for_images(tmp_path):
    seq = [_ts([A], text="")] * 2
    fetches, results = _run_image_loop(tmp_path, seq)
    assert results[0]["ok"] is True and len(fetches) == 1


@needs_node
def test_loop_text_mode_unchanged(tmp_path):
    kp = tmp_path / "keypress"
    body = build_script("hi", space="subllm-chatgpt-abcd1234", timeout_ms=600000, keypress_file=str(kp))
    runtime = _FAKE_RUNTIME.replace(*_FAST_CLOCK)
    for old, new in _IMG_RUNTIME_SWAPS[:3]:
        runtime = runtime.replace(old, new)
    runtime = runtime.replace("const __typed", "const __script = [" + json.dumps(_ts([A])) + "];\nconst __typed", 1)
    prog = tmp_path / "run.js"
    prog.write_text(runtime + "(async () => {\n" + body + "\n})();\n")
    p = subprocess.run(["node", str(prog)], capture_output=True, text=True, timeout=60)
    results = parse_output(p.stdout)[1]
    assert results[0]["ok"] is True and results[0]["images"] == [] and results[0]["text"] == "cap"
