import base64
import contextlib
import fcntl
import re
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from subllm.drivers.chatgpt_web import chrome as mod
from subllm.drivers.chatgpt_web.chrome import ChromeDriver
from subllm.drivers.chatgpt_web.ego import lock_path
from subllm.errors import (
    ClientError,
    HumanActionRequired,
    ModelDowngradeError,
    OutputError,
    QuotaError,
    SendUnconfirmedError,
)

from .test_chatgpt_web_ego import PNG_BYTES, URL

_REAL_SPAWN = mod._spawn_login
_REAL_OPEN = mod._open_context
_REAL_KILL = mod._kill_browser
_CORE = (Path(mod.__file__).parent / "page_core.js").read_text()

LOGGED_IN = {"logged_in": True, "challenge": False, "login_prompt": False, "quota": False, "mode_surface": "chat", "busy": False}
PENDING = {"state": "pending", "turn_key": None, "conversation_url": None}
ACCEPTED = {"state": "accepted", "turn_key": "t1", "conversation_url": URL}
DONE = {"done": True, "text": "hello", "stop_visible": False, "images": [], "error_dialog": None}
CAP = {"text": "hello", "format": "markdown"}


class Keyboard:
    def __init__(self, log, fail_on=None):
        self.log, self.fail_on = log, fail_on

    def type(self, text, delay=0):
        self.log.append(("type", text, delay))
        if self.fail_on == "type":
            raise RuntimeError("target closed")

    def press(self, key):
        self.log.append(("press", key))
        if self.fail_on == "press:" + key:
            raise RuntimeError("target closed")


class Page:
    def __init__(self, script, log, fetch=None, fail_on=None, urls=None):
        self.script, self.log, self.fetch = dict(script), log, fetch or {}
        self.fail_on = fail_on
        self.keyboard = Keyboard(log, fail_on)
        self.worlds = []
        self.calls = []
        self.url = "https://chatgpt.com/"
        self.urls = urls

    def bring_to_front(self):
        self.log.append("front")

    def goto(self, url, **kw):
        self.log.append(("goto", url, kw))
        if self.fail_on == "goto-timeout":
            raise type("TimeoutError", (Exception,), {})("Timeout 15000ms exceeded")

    def focus(self, sel):
        self.log.append(("focus", sel))

    def click(self, sel, **kw):
        self.log.append(("click", sel))
        if self.fail_on == "click":
            raise RuntimeError("element detached")

    def evaluate(self, expr, isolated_context=True):
        if expr.startswith("!!document.querySelector"):
            return True
        if expr.startswith("(async (src)"):
            src = re.search(r"\}\)\((.*)\)$", expr, re.S).group(1)
            self.worlds.append(("fetch", isolated_context))
            v = self.fetch[eval(src)]
            if isinstance(v, Exception):
                raise v
            return v
        name = re.match(r"\w+", expr[len(_CORE) + 3 :]).group(0)
        self.worlds.append((name, isolated_context))
        self.calls.append(name)
        v = self.script[name]
        if callable(v):
            return v()
        if isinstance(v, list):
            return v.pop(0) if len(v) > 1 else v[0]
        return v


class Ctx:
    def __init__(self, page):
        self.pages = [page]


@pytest.fixture
def chrome(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    now = [0.0]
    monkeypatch.setattr(mod, "_clock", lambda: now[0])
    monkeypatch.setattr(mod, "_sleep", lambda s: now.__setitem__(0, now[0] + s))
    log: list = []
    state = {}

    def install(script=None, **kw):
        full = {
            "state": LOGGED_IN,
            "composerMatches": True,
            "acceptance": ACCEPTED,
            "turnStatus": DONE,
            "captureText": CAP,
            "servedModel": "gpt-x",
        }
        full.update(script or {})
        page = Page(full, log, **kw)

        @contextlib.contextmanager
        def fake_open(profile):
            log.append(("open", profile))
            try:
                yield Ctx(page)
            finally:
                log.append("close")

        def fake_spawn():
            log.append("spawn")
            if state.get("spawn_error"):
                raise OSError("no exec")

        monkeypatch.setattr(mod, "_open_context", fake_open)
        monkeypatch.setattr(mod, "_spawn_login", fake_spawn)
        return page

    install.log = log
    install.state = state
    install.now = now
    return install


def run(prompt="hi", timeout_s=5):
    return ChromeDriver().run_text(prompt, timeout_s=timeout_s)


def typed(log):
    return [e for e in log if isinstance(e, tuple) and e[0] in ("type", "press")]


def test_success_text(chrome):
    page = chrome()
    r = run("hello there")
    assert (r.text, r.format, r.served_model, r.conversation_url) == ("hello", "markdown", "gpt-x", URL)
    assert ("type", "hello there", 40) in chrome.log
    assert chrome.log.index(("press", "Enter")) > chrome.log.index(("type", "hello there", 40))
    assert chrome.log[-1] == "close"
    assert ("goto", "https://chatgpt.com/", {"wait_until": "domcontentloaded", "timeout": 15000}) in chrome.log
    assert chrome.log[0][0] == "open" and str(chrome.log[0][1]).endswith(".local/share/subllm/chrome-profile")


def test_multiline_prompt_uses_shift_enter(chrome):
    chrome()
    run("a\nb\r\nc\td")
    assert typed(chrome.log) == [
        ("type", "a", 40),
        ("press", "Shift+Enter"),
        ("type", "b", 40),
        ("press", "Shift+Enter"),
        ("type", "c    d", 40),
        ("press", "Enter"),
    ]


def test_plain_format_when_capture_is_not_markdown(chrome):
    chrome({"captureText": {"text": "x", "format": "text"}})
    assert run().format == "plain"


def test_empty_prompt_is_client_error_before_launch(chrome):
    chrome()
    with pytest.raises(ClientError, match="empty"):
        run("  \n")
    assert chrome.log == []


def test_capture_runs_in_main_world_and_reads_in_isolated(chrome):
    page = chrome()
    run()
    worlds = dict(page.worlds)
    assert worlds["captureText"] is False
    for name in ("state", "composerMatches", "acceptance", "turnStatus", "servedModel"):
        assert worlds[name] is True


def test_exception_in_first_type_is_unconfirmed(chrome):
    chrome(fail_on="type")
    with pytest.raises(SendUnconfirmedError):
        run()
    assert chrome.log[-1] == "close"


def test_exception_before_typing_is_client_error(chrome):
    chrome(fail_on="click")
    with pytest.raises(ClientError) as ei:
        run()
    assert not isinstance(ei.value, SendUnconfirmedError)
    assert typed(chrome.log) == []


def test_exception_on_enter_is_unconfirmed(chrome):
    chrome(fail_on="press:Enter")
    with pytest.raises(SendUnconfirmedError):
        run()


def test_goto_timeout_is_ignored(chrome):
    chrome(fail_on="goto-timeout")
    assert run().text == "hello"


def test_goto_other_error_is_client_error(chrome, monkeypatch):
    page = chrome()
    page.goto = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net::ERR_FAILED"))
    with pytest.raises(ClientError, match="ERR_FAILED"):
        run()
    assert typed(chrome.log) == []


def test_login_wall_closes_context_then_spawns(chrome):
    chrome({"state": {**LOGGED_IN, "logged_in": False, "login_prompt": True}})
    with pytest.raises(HumanActionRequired) as ei:
        run()
    assert ei.value.browser == "chrome"
    assert "close the window" in ei.value.detail and "log in to chatgpt.com" in ei.value.detail
    assert chrome.log[-2:] == ["close", "spawn"]
    assert chrome.now[0] >= 45
    assert typed(chrome.log) == []


def test_login_spawn_failure_still_raises_human_action(chrome):
    chrome({"state": {**LOGGED_IN, "logged_in": False, "login_prompt": True}})
    chrome.state["spawn_error"] = True
    with pytest.raises(HumanActionRequired) as ei:
        run()
    assert "could not be opened" in ei.value.detail
    assert chrome.log[-2:] == ["close", "spawn"]


def test_challenge_is_human_action(chrome):
    chrome({"state": {**LOGGED_IN, "logged_in": False, "challenge": True}})
    with pytest.raises(HumanActionRequired) as ei:
        run()
    assert ei.value.browser == "chrome"
    assert "Cloudflare" in ei.value.detail
    assert "solve the challenge" in ei.value.detail and "log in" not in ei.value.detail
    assert chrome.now[0] < 5


def test_not_ready_is_client_error_without_handoff(chrome):
    chrome({"state": {**LOGGED_IN, "logged_in": False}})
    with pytest.raises(ClientError, match="did not become ready"):
        run()
    assert "spawn" not in chrome.log


def test_state_errors_are_tolerated_until_the_page_settles(chrome):
    seq = [RuntimeError("Execution context was destroyed"), LOGGED_IN]

    def state():
        v = seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    chrome({"state": state})
    assert run().text == "hello"


def test_state_never_available_is_client_error(chrome):
    def state():
        raise RuntimeError("boom")

    chrome({"state": state})
    with pytest.raises(ClientError, match="page state unavailable: boom"):
        run()


def test_quota_before_send(chrome):
    chrome({"state": {**LOGGED_IN, "quota": True}})
    with pytest.raises(QuotaError):
        run()
    assert typed(chrome.log) == []


def test_work_mode(chrome):
    chrome({"state": {**LOGGED_IN, "mode_surface": "work"}})
    with pytest.raises(ClientError, match="Work mode"):
        run()


def test_busy_and_existing_chat(chrome):
    chrome({"state": {**LOGGED_IN, "busy": True}})
    with pytest.raises(ClientError, match="busy"):
        run()
    page = chrome()
    page.url = "https://chatgpt.com/c/abc"
    with pytest.raises(ClientError, match="not a new chat"):
        run()
    assert typed(chrome.log) == []


def test_composer_mismatch_is_unconfirmed(chrome):
    chrome({"composerMatches": False})
    with pytest.raises(SendUnconfirmedError):
        run()
    assert ("press", "Enter") not in chrome.log


def test_chat_opened_while_typing_is_unconfirmed(chrome):
    page = chrome()
    orig = page.keyboard.type

    def type_and_navigate(text, delay=0):
        orig(text, delay)
        page.url = "https://chatgpt.com/c/early"

    page.keyboard.type = type_and_navigate
    with pytest.raises(SendUnconfirmedError, match="chat opened"):
        run()
    assert ("press", "Enter") not in chrome.log


def test_rejected_acceptance_is_client_error_not_resent(chrome):
    chrome({"acceptance": {"state": "rejected", "turn_key": None, "conversation_url": None}})
    with pytest.raises(ClientError, match="not resent") as ei:
        run()
    assert not isinstance(ei.value, SendUnconfirmedError)
    assert chrome.log.count(("press", "Enter")) == 1


def test_acceptance_pending_at_30s_is_unconfirmed(chrome):
    chrome({"acceptance": PENDING})
    with pytest.raises(SendUnconfirmedError) as ei:
        run()
    assert ei.value.conversation_url is None
    assert 30 <= chrome.now[0] < 40


def test_exception_after_accept_carries_the_url(chrome):
    def status():
        raise RuntimeError("target crashed")

    chrome({"turnStatus": status})
    with pytest.raises(SendUnconfirmedError) as ei:
        run()
    assert ei.value.conversation_url == URL
    assert chrome.log[-1] == "close"


def test_answer_deadline_is_unconfirmed_with_url(chrome):
    chrome({"turnStatus": {**DONE, "done": False}})
    with pytest.raises(SendUnconfirmedError) as ei:
        run(timeout_s=3)
    assert ei.value.conversation_url == URL


def test_error_dialog_after_send_is_unconfirmed_and_before_is_not_reachable(chrome):
    chrome({"turnStatus": {**DONE, "done": False, "error_dialog": {"kind": "quota", "text": "limit"}}})
    with pytest.raises(SendUnconfirmedError):
        run()


def test_empty_answer_is_output_error(chrome):
    chrome({"captureText": {"text": "  ", "format": "plain"}})
    with pytest.raises(OutputError):
        run()


def test_waits_for_done_and_passes_previous_text(chrome):
    seen = []
    seq = [{**DONE, "done": False, "text": "he"}, DONE]

    def status():
        v = seq.pop(0) if len(seq) > 1 else seq[0]
        seen.append(v["done"])
        return v

    chrome({"turnStatus": status})
    assert run().text == "hello"
    assert seen == [False, True]


IMG_URL = "blob:https://chatgpt.com/abc"
IMG_DONE = {**DONE, "text": "", "images": [IMG_URL]}


def b64(b):
    return base64.b64encode(b).decode()


def run_image(out, timeout_s=5):
    return ChromeDriver().run_image("draw", out, timeout_s=timeout_s)


def test_image_bytes_are_decoded_and_saved(chrome, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    page = chrome({"turnStatus": IMG_DONE, "captureText": {"text": "", "format": "plain"}}, fetch={IMG_URL: b64(PNG_BYTES)})
    r = run_image(out)
    assert r.route == "web" and r.conversation_url == URL
    assert len(r.paths) == 1 and r.paths[0].parent == out and r.paths[0].suffix == ".png"
    assert r.paths[0].read_bytes() == PNG_BYTES
    assert dict(page.worlds)["fetch"] is False


def test_image_waits_for_a_stable_list(chrome, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    seq = [{**IMG_DONE, "images": ["blob:x/preview"]}, IMG_DONE, IMG_DONE]
    calls = []

    def status():
        calls.append(1)
        return seq.pop(0) if len(seq) > 1 else seq[0]

    chrome({"turnStatus": status}, fetch={IMG_URL: b64(PNG_BYTES)})
    assert run_image(out).paths[0].read_bytes() == PNG_BYTES
    assert len(calls) == 3


def test_image_not_an_image_is_unconfirmed(chrome, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    chrome({"turnStatus": IMG_DONE}, fetch={IMG_URL: b64(b"<html>not an image</html>")})
    with pytest.raises(SendUnconfirmedError) as ei:
        run_image(out)
    assert ei.value.conversation_url == URL
    assert list(out.iterdir()) == []


def test_image_fetch_failure_after_send_is_unconfirmed(chrome, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    chrome({"turnStatus": IMG_DONE}, fetch={IMG_URL: RuntimeError("image fetch failed: 404")})
    with pytest.raises(SendUnconfirmedError, match="404") as ei:
        run_image(out)
    assert ei.value.conversation_url == URL


def test_image_refusal_after_grace_is_output_error(chrome, tmp_path):
    chrome({"turnStatus": {**DONE, "images": []}})
    with pytest.raises(OutputError):
        run_image(tmp_path, timeout_s=60)
    assert 8 <= chrome.now[0] < 20


def test_image_write_failure_after_send_is_unconfirmed(chrome, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    chrome({"turnStatus": IMG_DONE}, fetch={IMG_URL: b64(PNG_BYTES)})
    out.chmod(0o500)
    try:
        with pytest.raises(SendUnconfirmedError, match="not saved"):
            run_image(out)
    finally:
        out.chmod(0o700)


def test_tmpdir_is_removed(chrome, tmp_path, monkeypatch):
    made = []
    real = mod.tempfile.mkdtemp
    monkeypatch.setattr(mod.tempfile, "mkdtemp", lambda **kw: made.append(real(**kw)) or made[-1])
    chrome()
    run()
    assert made and not Path(made[0]).exists()


def test_launch_failure_is_client_error_naming_the_profile(chrome, monkeypatch):
    @contextlib.contextmanager
    def boom(profile):
        raise RuntimeError("profile locked")
        yield

    chrome()
    monkeypatch.setattr(mod, "_open_context", boom)
    with pytest.raises(ClientError, match="another Chrome window"):
        run()


def test_other_launch_failure_passes_the_cause_through_plainly(chrome, monkeypatch):
    @contextlib.contextmanager
    def boom(profile):
        raise RuntimeError("Executable doesn't exist at /x")
        yield

    chrome()
    monkeypatch.setattr(mod, "_open_context", boom)
    with pytest.raises(ClientError) as ei:
        run()
    assert "Executable doesn't exist" in str(ei.value) and "another Chrome window" not in str(ei.value)


def test_held_lock_blocks_before_launch(chrome, tmp_path):
    chrome()
    p = lock_path()
    p.parent.mkdir(parents=True)
    with open(p, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        with pytest.raises(ClientError, match="another chatgpt-web call"):
            run(timeout_s=0.3)
    assert chrome.log == []


def test_lock_released_after_a_call(chrome):
    chrome()
    run()
    with open(lock_path(), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_available_needs_patchright(monkeypatch):
    monkeypatch.setitem(sys.modules, "patchright", None)
    monkeypatch.setitem(sys.modules, "patchright.sync_api", None)
    ok, reason = ChromeDriver().available()
    assert not ok and "subllm[chrome]" in reason


def _fake_patchright(monkeypatch):
    fake = types.ModuleType("patchright")
    fake.sync_api = types.ModuleType("patchright.sync_api")
    monkeypatch.setitem(sys.modules, "patchright", fake)
    monkeypatch.setitem(sys.modules, "patchright.sync_api", fake.sync_api)


def test_available_needs_chrome(monkeypatch):
    _fake_patchright(monkeypatch)
    _fake_patchright(monkeypatch)
    monkeypatch.setattr(mod, "_chrome_executable", lambda: None)
    ok, reason = ChromeDriver().available()
    assert not ok and "Google Chrome is not installed at" in reason and "subllm[chrome]" in reason


def test_available_ok(monkeypatch):
    _fake_patchright(monkeypatch)
    monkeypatch.setattr(mod, "_chrome_executable", lambda: "/x/chrome")
    assert ChromeDriver().available() == (True, "ok")


def test_chrome_executable_mirrors_patchright_channel_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    fake = tmp_path / "Chrome"
    monkeypatch.setitem(mod._CHROME_PATHS, "darwin", str(fake))
    assert mod._chrome_executable() is None
    # ~/Applications and a PATH google-chrome are not where channel="chrome" looks
    user = tmp_path / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    user.parent.mkdir(parents=True)
    user.write_text("")
    monkeypatch.setattr(mod.shutil, "which", lambda n: str(user))
    assert mod._chrome_executable() is None
    fake.write_text("")
    assert mod._chrome_executable() == str(fake)


def test_chrome_candidates_per_platform(monkeypatch):
    monkeypatch.setattr(mod.sys, "platform", "linux")
    assert mod._chrome_candidates() == ["/opt/google/chrome/chrome"]
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    assert mod._chrome_candidates() == ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    monkeypatch.setattr(mod.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", "L")
    monkeypatch.setenv("PROGRAMFILES", "P")
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.setenv("HOMEDRIVE", "C:")
    assert mod._chrome_candidates() == [
        "L\\Google\\Chrome\\Application\\chrome.exe",
        "P\\Google\\Chrome\\Application\\chrome.exe",
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    ]
    monkeypatch.setattr(mod.sys, "platform", "freebsd")
    assert mod._chrome_candidates() == []


def test_check_guide_is_a_noop():
    assert ChromeDriver().check_guide() is None


def test_profile_is_under_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert mod.profile_dir() == tmp_path / ".local/share/subllm/chrome-profile"


def test_spawn_is_detached_module_login(monkeypatch):
    seen = {}

    class P:
        def __init__(self, argv, **kw):
            seen.update(argv=argv, **kw)

    monkeypatch.setattr(mod.subprocess, "Popen", P)
    _REAL_SPAWN()
    assert seen["argv"] == [sys.executable, "-m", "subllm.drivers.chatgpt_web.chrome", "login"]
    assert seen["start_new_session"] is True
    assert seen["stdin"] == seen["stdout"] == seen["stderr"] == subprocess.DEVNULL


def test_login_helper_opens_chatgpt_and_closes_context_when_the_page_closes(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    log = []

    class LoginPage:
        def goto(self, url, **kw):
            log.append(("goto", url))

        def wait_for_event(self, name, timeout=None):
            log.append(("page-wait", name, timeout))

    class LoginCtx:
        pages = []

        def new_page(self):
            return LoginPage()

        def wait_for_event(self, name, timeout=None):
            raise AssertionError("must wait on the page, not the browser process")

    @contextlib.contextmanager
    def fake_open(profile):
        log.append(("open", profile))
        yield LoginCtx()
        log.append("context-closed")

    monkeypatch.setattr(mod, "_open_context", fake_open)
    assert mod.main(["login"]) == 0
    assert log == [("open", mod.profile_dir()), ("goto", "https://chatgpt.com/"), ("page-wait", "close", 0), "context-closed"]
    assert mod.main(["nope"]) == 2


def test_image_fetch_script_is_bounded(chrome, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    page = chrome({"turnStatus": IMG_DONE}, fetch={IMG_URL: b64(PNG_BYTES)})
    seen = []
    orig = page.evaluate
    page.evaluate = lambda expr, **kw: (seen.append(expr), orig(expr, **kw))[1]
    run_image(out)
    script = next(e for e in seen if e.startswith("(async (src)"))
    assert "AbortController" in script and "signal" in script and "30000" in script


def test_image_fetch_timeout_is_unconfirmed_and_releases_the_lock(chrome, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    chrome({"turnStatus": IMG_DONE}, fetch={IMG_URL: RuntimeError("The operation was aborted")})
    with pytest.raises(SendUnconfirmedError, match="aborted") as ei:
        run_image(out)
    assert ei.value.conversation_url == URL
    with open(lock_path(), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_hermetic_guard_blocks_real_launch_and_spawn():
    with pytest.raises(pytest.fail.Exception, match="hermetic"):
        mod._open_context(Path("x"))
    with pytest.raises(pytest.fail.Exception, match="hermetic"):
        mod._spawn_login()


def test_real_open_context_uses_the_persistent_chrome_profile(monkeypatch, tmp_path):
    calls = {}

    class Chromium:
        def launch_persistent_context(self, **kw):
            calls.update(kw)

            class C:
                closed = False

                def close(self):
                    C.closed = True

            return C()

    class PW:
        chromium = Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    api = types.ModuleType("patchright.sync_api")
    api.sync_playwright = lambda: PW()
    pkg = types.ModuleType("patchright")
    pkg.sync_api = api
    monkeypatch.setitem(sys.modules, "patchright", pkg)
    monkeypatch.setitem(sys.modules, "patchright.sync_api", api)
    with _REAL_OPEN(tmp_path / "p") as ctx:
        assert ctx.closed is False
    assert ctx.closed is True
    assert calls == {"user_data_dir": str(tmp_path / "p"), "channel": "chrome", "headless": False, "no_viewport": True}


class Blocking:
    """A page whose core calls hang until the kill seam releases them."""

    def __init__(self, page, block_on):
        self.release = threading.Event()
        self.entered = threading.Event()
        orig = page.evaluate

        def evaluate(expr, **kw):
            if not expr.startswith("!!") and re.match(r"\w+", expr[len(_CORE) + 3 :] or "") and \
                    re.match(r"\w+", expr[len(_CORE) + 3 :]).group(0) == block_on:
                self.entered.set()
                if not self.release.wait(10):
                    raise AssertionError("the kill seam never ran (timer not armed)")
                raise RuntimeError("Target page, context or browser has been closed")
            return orig(expr, **kw)

        page.evaluate = evaluate


def _arm(monkeypatch, kills, block):
    monkeypatch.setattr(mod, "_budget", lambda *a: 0.2)
    monkeypatch.setattr(mod, "_kill_browser", lambda profile: (kills.append(profile), block.release.set()))
    # real clocks for the timer test: the flow itself never loops while blocked
    monkeypatch.setattr(mod, "_clock", time.monotonic)
    monkeypatch.setattr(mod, "_sleep", lambda s: None)


def test_frozen_renderer_before_send_is_client_error_and_browser_is_killed(chrome, monkeypatch):
    page = chrome()
    block = Blocking(page, "state")
    kills = []
    _arm(monkeypatch, kills, block)
    with pytest.raises(ClientError, match="Chrome timed out after 0s") as ei:
        run()
    assert not isinstance(ei.value, SendUnconfirmedError)
    assert kills == [mod.profile_dir()]
    assert typed(chrome.log) == [] and chrome.log[-1] == "close"
    with open(lock_path(), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_frozen_renderer_after_send_is_unconfirmed_with_url(chrome, monkeypatch):
    page = chrome()
    block = Blocking(page, "turnStatus")
    kills = []
    _arm(monkeypatch, kills, block)
    with pytest.raises(SendUnconfirmedError, match="Chrome timed out") as ei:
        run()
    assert ei.value.conversation_url == URL
    assert kills == [mod.profile_dir()]
    with open(lock_path(), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_timer_is_cancelled_after_a_normal_call(chrome, monkeypatch):
    kills = []
    monkeypatch.setattr(mod, "_budget", lambda *a: 0.2)
    monkeypatch.setattr(mod, "_kill_browser", lambda profile: kills.append(profile))
    chrome()
    run()
    time.sleep(0.4)
    assert kills == []


def test_budget_matches_the_ego_formula():
    assert mod._budget(100, "x" * 100, "text") == 100 + 120 + 6
    assert mod._budget(100, "", "image") == 100 + 120 + 60


def test_browser_pid_comes_from_the_profile_singleton_lock(tmp_path):
    assert mod._browser_pid(tmp_path) is None
    (tmp_path / "SingletonLock").symlink_to("my-host-name-4242")
    assert mod._browser_pid(tmp_path) == 4242
    (tmp_path / "SingletonLock").unlink()
    (tmp_path / "SingletonLock").symlink_to("host-notapid")
    assert mod._browser_pid(tmp_path) is None


def test_kill_signals_only_the_profile_pid(monkeypatch, tmp_path):
    (tmp_path / "SingletonLock").symlink_to("h-4242")
    sent = []
    monkeypatch.setattr(mod.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    _REAL_KILL(tmp_path)
    assert sent == [(4242, mod.signal.SIGKILL)]
    sent.clear()
    _REAL_KILL(tmp_path / "other")
    assert sent == []


def test_call_does_not_finish_while_the_kill_callback_is_running(chrome, monkeypatch):
    page = chrome()
    orig = page.evaluate
    state = {"entered": threading.Event(), "done": False}

    def slow(expr, **kw):
        if expr[len(_CORE) + 3 :].startswith("captureText"):
            assert state["entered"].wait(5), "timer never fired"
        return orig(expr, **kw)

    page.evaluate = slow

    def kill(profile):
        state["entered"].set()
        time.sleep(0.4)
        state["done"] = True

    monkeypatch.setattr(mod, "_budget", lambda *a: 0.1)
    monkeypatch.setattr(mod, "_kill_browser", kill)
    assert run().text == "hello"
    assert state["done"], "the call returned while the kill callback was still running"
    time.sleep(0.2)


CLICK = {"action": "click", "selector": "button[aria-label='Select ChatGPT model']", "current": "Medium"}
FOCUS = {"action": "focus", "selector": "[role=menu] [data-reasoning-slider]", "current": "Medium"}
RIGHT = {"action": "key", "key": "ArrowRight", "current": "Medium"}
ESC = {"action": "key", "key": "Escape", "current": "Medium"}
DONE_PRO = {"action": "done", "selected_label": "Pro", "current": "Pro"}
DONE_MED = {"action": "done", "selected_label": "Medium", "current": "Medium"}
RESTORE = [
    {"action": "click", "selector": "s", "current": "Pro"},
    {"action": "key", "key": "ArrowLeft", "current": "Pro"},
    DONE_MED,
]
PICK_PRO = [CLICK, FOCUS, RIGHT, RIGHT, RIGHT, RIGHT, ESC, DONE_PRO]


def picker_log(log):
    return [e for e in log if isinstance(e, tuple) and e[0] in ("click", "focus", "press")]


def test_default_mode_never_calls_the_picker(chrome):
    page = chrome({"selectMode": [DONE_PRO], "auditModel": "downgrade"})
    run()
    assert "selectMode" not in page.calls and "auditModel" not in page.calls
    assert not [e for e in chrome.log if e[0:1] == ("focus",)]


def test_pro_mode_selects_with_real_input_before_the_composer(chrome):
    page = chrome({"selectMode": PICK_PRO + RESTORE, "auditModel": "ok", "servedModel": "GPT-6 Pro"})
    r = ChromeDriver().run_text("hi", timeout_s=5, mode="pro")
    assert r.text == "hello"
    acts = picker_log(chrome.log)
    assert acts[:7] == [
        ("click", CLICK["selector"]), ("focus", FOCUS["selector"]),
        ("press", "ArrowRight"), ("press", "ArrowRight"), ("press", "ArrowRight"), ("press", "ArrowRight"),
        ("press", "Escape"),
    ]
    assert acts[7] == ("click", "div.ProseMirror[role=textbox]")
    assert chrome.log.index(("type", "hi", 40)) > chrome.log.index(("press", "Escape"))
    assert page.calls.count("selectMode") == 8 + 3 and page.calls.count("auditModel") == 1
    assert ("press", "ArrowLeft") in chrome.log


def test_picker_fail_is_client_error_before_any_key_event(chrome):
    chrome({"selectMode": [{"action": "fail", "detail": "picker is locked", "current": None}]})
    with pytest.raises(ClientError, match="mode selection failed: picker is locked") as ei:
        ChromeDriver().run_text("hi", timeout_s=5, mode="pro")
    assert type(ei.value) is ClientError
    assert not typed(chrome.log)


def test_picker_step_bound_is_client_error_and_restore_is_bounded(chrome):
    page = chrome({"selectMode": [RIGHT]})
    with pytest.raises(ClientError, match="mode selection unconfirmed"):
        ChromeDriver().run_text("hi", timeout_s=5, mode="pro")
    assert page.calls.count("selectMode") == 32
    assert not [e for e in chrome.log if e[0:1] == ("type",)] and ("press", "Enter") not in chrome.log


@pytest.mark.parametrize(
    "bad,exc",
    [
        ({**LOGGED_IN, "mode_surface": "work"}, ClientError),
        ({**LOGGED_IN, "busy": True}, ClientError),
        ({**LOGGED_IN, "logged_in": False}, ClientError),
        ({**LOGGED_IN, "quota": True}, QuotaError),
    ],
)
def test_post_selection_state_must_be_chat(chrome, bad, exc):
    chrome({"selectMode": [DONE_PRO, DONE_MED], "state": [LOGGED_IN, bad]})
    with pytest.raises(exc) as ei:
        ChromeDriver().run_text("hi", timeout_s=5, mode="pro")
    assert type(ei.value) is exc
    assert not typed(chrome.log)


def test_downgrade_carries_text_served_model_and_requested_mode(chrome):
    chrome({"selectMode": [DONE_PRO, DONE_MED], "auditModel": "downgrade", "servedModel": "GPT-6 Thinking"})
    with pytest.raises(ModelDowngradeError) as ei:
        ChromeDriver().run_text("hi", timeout_s=5, mode="pro")
    e = ei.value
    assert (e.text, e.served_model, e.requested_mode) == ("hello", "GPT-6 Thinking", "pro")


def test_downgrade_audit_runs_before_the_image_fetch(chrome, tmp_path):
    chrome(
        {"selectMode": [DONE_PRO, DONE_MED], "auditModel": "downgrade", "turnStatus": {**DONE, "images": ["blob:a"]}},
        fetch={"blob:a": "AAAA"},
    )
    with pytest.raises(ModelDowngradeError):
        ChromeDriver().run_image("hi", tmp_path / "o.png", timeout_s=5, mode="pro")
    assert not (tmp_path / "o.png").exists()


def test_restore_runs_when_the_level_changed_even_after_a_failure(chrome):
    page = chrome({"selectMode": [CLICK, DONE_PRO] + RESTORE, "auditModel": "downgrade"})
    with pytest.raises(ModelDowngradeError):
        ChromeDriver().run_text("hi", timeout_s=5, mode="pro")
    assert page.calls.count("selectMode") == 5 and ("press", "ArrowLeft") in chrome.log
    assert chrome.log[-1] == "close"


def test_restore_is_skipped_when_the_level_is_unchanged(chrome):
    page = chrome({"selectMode": [DONE_PRO], "auditModel": "ok"})
    ChromeDriver().run_text("hi", timeout_s=5, mode="pro")
    assert page.calls.count("selectMode") == 1


def test_throwing_restore_does_not_change_the_result(chrome):
    def boom():
        raise RuntimeError("target closed")

    calls = [CLICK, DONE_PRO]
    page = chrome({"selectMode": lambda: calls.pop(0) if calls else boom(), "auditModel": "ok"})
    assert ChromeDriver().run_text("hi", timeout_s=5, mode="pro").text == "hello"
    assert page.calls.count("selectMode") == 3


def test_restore_is_skipped_when_the_browser_is_dead(chrome):
    rec = mod._Rec()
    rec.dead = True
    page = chrome({"selectMode": [CLICK, DONE_PRO] + RESTORE, "auditModel": "ok"})
    mod._flow(Ctx(page), "hi", 5, "text", Path("/nonexistent"), rec, "pro")
    assert page.calls.count("selectMode") == 2
