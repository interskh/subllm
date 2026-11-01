import fcntl
import json
import os
import re
import shutil
import subprocess

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


def test_early_chat_branch_writes_record_before_stopping():
    body = script()
    i = body.index("chat opened before the send key")
    assert body.rindex("record();", 0, i) > body.rindex("isChat()", 0, i)


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
    if (call.includes(").state(")) return { logged_in: true, mode_surface: "chat", busy: false, quota: false };
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
