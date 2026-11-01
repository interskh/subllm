import json
import os
import sys

import pytest

from subllm import ChatGPTWebLLM, RegionGuard, WebTextResult
from subllm.base import ImageGenerator, ImageResult
from subllm import chatgpt_web as cw
from subllm.errors import (
    ClientError,
    HumanActionRequired,
    OutputError,
    QuotaError,
    RegionError,
    SendUnconfirmedError,
)

from .test_chatgpt_web_ego import IMG_STUB, OK, PNG_BYTES, STUB, URL, ev, fail, img_ok, res

# Records every ego-browser call (detection included) before the shared stub runs.
RECORDING_STUB = STUB.replace(
    "case \"$1\" in", 'echo "$@" >> "$STUB_DIR/calls"\ncase "$1" in', 1
)


@pytest.fixture
def ego(fake_bin, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / "stub"
    d.mkdir()
    monkeypatch.setenv("STUB_DIR", str(d))
    fake_bin("ego-browser", RECORDING_STUB)

    def scenario(stdout="", *, keypress=True, code=0):
        monkeypatch.setenv("STUB_STDERR", "")
        monkeypatch.setenv("STUB_STDOUT", stdout)
        monkeypatch.setenv("STUB_KEYPRESS", "1" if keypress else "0")
        monkeypatch.setenv("STUB_CODE", str(code))

    scenario.dir = d
    scenario(ev(phase="accepted", conversation_url=URL) + "\n" + OK)
    return scenario


def _ok(text):
    return "\n".join([
        ev(phase="accepted", conversation_url=URL),
        res(ok=True, text=text, format="plain", images=[], served_model=None, conversation_url=URL),
    ])


def test_complete_result(ego):
    r = ChatGPTWebLLM().complete_result("hi")
    assert isinstance(r, WebTextResult)
    assert (r.text, r.format, r.served_model, r.conversation_url) == ("hello", "markdown", "gpt-x", URL)


def test_complete_returns_text(ego):
    assert ChatGPTWebLLM(browser="ego").complete("hi") == "hello"


def test_complete_json(ego):
    ego(_ok('{"a": 1}'))
    assert ChatGPTWebLLM().complete_json("q") == {"a": 1}


def test_complete_json_appends_instruction(ego):
    ego(_ok('{"a": 1}'))
    ChatGPTWebLLM().complete_json("MYQUESTION")
    script = (ego.dir / "script.js").read_text()
    assert "MYQUESTION" in script and "Return ONLY a single JSON object" in script


def test_complete_json_invalid(ego):
    ego(_ok("not json"))
    with pytest.raises(OutputError):
        ChatGPTWebLLM().complete_json("q")


def test_empty_text_is_output_error(ego):
    ego(_ok("  \n "))
    with pytest.raises(OutputError):
        ChatGPTWebLLM().complete("q")


def test_guide_v3_under_auto_is_client_error_not_fallback(ego, monkeypatch):
    monkeypatch.setenv("STUB_GUIDE", "v3.0.0")
    with pytest.raises(ClientError, match="supported"):
        ChatGPTWebLLM(browser="auto").complete("q")
    assert not (ego.dir / "ran").exists()


def test_region_guard_runs_before_any_ego_call(ego):
    def lookup():
        return "XX"

    guard = RegionGuard(allowed_regions={"US"}, lookup=lookup)
    with pytest.raises(RegionError):
        ChatGPTWebLLM(region_guard=guard).complete("q")
    assert not (ego.dir / "calls").exists()
    assert not (ego.dir / "script.js").exists()


CHROME_MISSING = "patchright is not installed (pip install 'subllm[chrome]')"


class FakeChrome:
    """Replaces ChromeDriver in subllm.chatgpt_web; records every call."""

    ok = True
    reason = CHROME_MISSING
    run_error: Exception | None = None
    calls: list = []
    kwargs: list = []

    def __init__(self):
        FakeChrome.calls.append("init")

    def available(self):
        FakeChrome.calls.append("available")
        return (True, "ok") if FakeChrome.ok else (False, FakeChrome.reason)

    def check_guide(self):
        FakeChrome.calls.append("check_guide")

    def run_text(self, prompt, *, timeout_s, mode="default"):
        FakeChrome.calls.append("run_text")
        FakeChrome.kwargs.append({"timeout_s": timeout_s, "mode": mode})
        if FakeChrome.run_error:
            raise FakeChrome.run_error
        return WebTextResult(text="from chrome", format="plain", served_model=None, conversation_url=URL)

    def run_image(self, prompt, dest, *, timeout_s, mode="default"):
        FakeChrome.calls.append("run_image")
        FakeChrome.kwargs.append({"timeout_s": timeout_s, "mode": mode})
        if FakeChrome.run_error:
            raise FakeChrome.run_error
        p = dest / "chrome-1.png"
        p.write_bytes(PNG_BYTES)
        return ImageResult(paths=[p], route="web", text="chrome", conversation_url=URL, served_model=None)


@pytest.fixture
def chrome(monkeypatch):
    FakeChrome.ok = True
    FakeChrome.reason = CHROME_MISSING
    FakeChrome.run_error = None
    FakeChrome.calls = []
    FakeChrome.kwargs = []
    monkeypatch.setattr(cw, "ChromeDriver", FakeChrome)
    return FakeChrome


@pytest.fixture
def no_ego(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path))


def test_chrome_only_uses_chrome_and_never_ego(ego, chrome):
    assert ChatGPTWebLLM(browser="chrome").complete("q") == "from chrome"
    assert chrome.calls == ["init", "available", "check_guide", "run_text"]
    assert not (ego.dir / "calls").exists()


def test_chrome_unavailable_names_the_extra(ego, chrome):
    chrome.ok = False
    with pytest.raises(ClientError, match=r"subllm\[chrome\]"):
        ChatGPTWebLLM(browser="chrome").complete("q")
    assert "run_text" not in chrome.calls and not (ego.dir / "calls").exists()


def _patchright_without_chrome(monkeypatch):
    import types

    from subllm.drivers.chatgpt_web import chrome as chrome_mod

    pkg = types.ModuleType("patchright")
    pkg.sync_api = types.ModuleType("patchright.sync_api")
    monkeypatch.setitem(sys.modules, "patchright", pkg)
    monkeypatch.setitem(sys.modules, "patchright.sync_api", pkg.sync_api)
    monkeypatch.setattr(chrome_mod, "_chrome_executable", lambda: None)


def test_chrome_not_installed_real_driver(ego, monkeypatch):
    _patchright_without_chrome(monkeypatch)
    with pytest.raises(ClientError, match=r"Google Chrome is not installed.*subllm\[chrome\]"):
        ChatGPTWebLLM(browser="chrome").complete("q")
    assert not (ego.dir / "calls").exists()


def test_auto_both_unavailable_names_the_extra_when_chrome_is_missing(no_ego, monkeypatch):
    _patchright_without_chrome(monkeypatch)
    with pytest.raises(ClientError, match=r"no browser is available.*subllm\[chrome\]"):
        ChatGPTWebLLM().complete("q")


def test_chrome_patchright_not_installed_real_driver(ego, monkeypatch):
    monkeypatch.setitem(sys.modules, "patchright", None)
    monkeypatch.setitem(sys.modules, "patchright.sync_api", None)
    with pytest.raises(ClientError, match=r"subllm\[chrome\]"):
        ChatGPTWebLLM(browser="chrome").complete("q")
    assert not (ego.dir / "calls").exists()


def test_auto_ego_passes_chrome_untouched(ego, chrome):
    assert ChatGPTWebLLM(browser="auto").complete("q") == "hello"
    assert chrome.calls == []


def test_ego_missing(no_ego):
    with pytest.raises(ClientError, match="not on PATH"):
        ChatGPTWebLLM(browser="ego").complete("q")


def test_ego_forced_never_falls_back(no_ego, chrome):
    with pytest.raises(ClientError, match="ego lite is not available"):
        ChatGPTWebLLM(browser="ego").complete("q")
    assert chrome.calls == []


def test_auto_ego_missing_falls_back_to_chrome_text(no_ego, chrome):
    llm = ChatGPTWebLLM()
    assert llm.complete("q") == "from chrome"
    assert llm.complete("q") == "from chrome"
    assert chrome.calls == ["init", "available", "check_guide", "run_text", "check_guide", "run_text"]


def test_auto_ego_missing_falls_back_to_chrome_image(no_ego, chrome, tmp_path):
    r = ChatGPTWebLLM().generate_image("a cube", tmp_path / "out")
    assert r.text == "chrome" and r.paths[0].read_bytes() == PNG_BYTES
    assert "run_image" in chrome.calls


def test_auto_both_unavailable_names_both_reasons(no_ego, chrome):
    chrome.ok = False
    with pytest.raises(ClientError) as ei:
        ChatGPTWebLLM().complete("q")
    msg = str(ei.value)
    assert "not on PATH" in msg and "subllm[chrome]" in msg
    assert "run_text" not in chrome.calls


def test_auto_both_unavailable_chrome_not_installed(no_ego, monkeypatch):
    monkeypatch.setitem(sys.modules, "patchright", None)
    monkeypatch.setitem(sys.modules, "patchright.sync_api", None)
    with pytest.raises(ClientError, match=r"not on PATH.*subllm\[chrome\]"):
        ChatGPTWebLLM().complete("q")


def test_failed_resolution_is_not_cached(no_ego, chrome):
    chrome.ok = False
    llm = ChatGPTWebLLM()
    with pytest.raises(ClientError):
        llm.complete("q")
    chrome.ok = True
    assert llm.complete("q") == "from chrome"


def test_auto_guide_v3_is_an_error_not_a_chrome_fallback(ego, chrome, monkeypatch):
    monkeypatch.setenv("STUB_GUIDE", "v3.0.0")
    with pytest.raises(ClientError, match="supported"):
        ChatGPTWebLLM(browser="auto").complete("q")
    assert chrome.calls == []


@pytest.mark.parametrize("scenario", ["client", "quota", "human", "unconfirmed"])
def test_auto_ego_run_error_propagates_without_a_second_driver(ego, chrome, scenario):
    if scenario == "client":
        ego(fail("client", "bad"), keypress=False, code=1)
        exc = ClientError
    elif scenario == "quota":
        ego(fail("quota"), keypress=False, code=1)
        exc = QuotaError
    elif scenario == "human":
        ego(fail("human_action", "log in"), keypress=False, code=1)
        exc = HumanActionRequired
    else:
        ego(ev(phase="accepted", conversation_url=URL), keypress=True, code=1)
        exc = SendUnconfirmedError
    with pytest.raises(exc):
        ChatGPTWebLLM(browser="auto").complete("q")
    assert chrome.calls == []


def test_auto_ego_image_error_does_not_try_chrome(egoimg, chrome, monkeypatch):
    d, out = egoimg
    monkeypatch.setenv("STUB_STDOUT", fail("quota"))
    monkeypatch.setenv("STUB_KEYPRESS", "0")
    monkeypatch.setenv("STUB_CODE", "1")
    with pytest.raises(QuotaError):
        ChatGPTWebLLM().generate_image("x", out)
    assert chrome.calls == []


@pytest.mark.parametrize("err", [ClientError("x"), QuotaError("x"), SendUnconfirmedError("x")])
def test_chrome_run_error_propagates_after_fallback(no_ego, chrome, err):
    chrome.run_error = err
    with pytest.raises(type(err)):
        ChatGPTWebLLM().complete("q")
    assert chrome.calls.count("run_text") == 1


def test_image_bad_out_dir_precedes_driver_resolution(no_ego, chrome, tmp_path):
    f = tmp_path / "file"
    f.write_text("x")
    with pytest.raises(ClientError, match="out_dir"):
        ChatGPTWebLLM().generate_image("x", f)
    assert chrome.calls == []


def test_region_guard_precedes_fallback_detection(no_ego, chrome):
    guard = RegionGuard(allowed_regions={"US"}, lookup=lambda: "XX")
    with pytest.raises(RegionError):
        ChatGPTWebLLM(region_guard=guard).complete("q")
    assert chrome.calls == []


@pytest.mark.parametrize(
    "kwargs",
    [{"mode": "turbo"}, {"plan": "gold"}, {"browser": "firefox"}],
)
def test_bad_args(kwargs):
    with pytest.raises(ValueError):
        ChatGPTWebLLM(**kwargs)


def test_pro_mode_needs_pro_plan():
    with pytest.raises(ValueError, match=r"mode 'pro' needs plan='pro' \(got 'plus'\)"):
        ChatGPTWebLLM(mode="pro", plan="plus")
    with pytest.raises(ValueError, match="'pro'.*'free'"):
        ChatGPTWebLLM(mode="pro", plan="free")
    with pytest.raises(ValueError, match="'pro'.*'plus'"):
        ChatGPTWebLLM(mode="pro")
    ChatGPTWebLLM(mode="pro", plan="pro")


@pytest.mark.parametrize("plan", ["free", "plus", "pro"])
def test_thinking_mode_on_any_plan(plan):
    ChatGPTWebLLM(mode="thinking", plan=plan)


def test_invalid_plan_gets_the_plan_message_even_with_pro_mode():
    with pytest.raises(ValueError, match="plan must be one of"):
        ChatGPTWebLLM(mode="pro", plan="gold")


def test_bad_mode_message():
    with pytest.raises(ValueError, match="mode must be one of"):
        ChatGPTWebLLM(mode="turbo")


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"mode": "pro", "plan": "pro"}, 2700.0),
        ({"mode": "default"}, 600.0),
        ({"mode": "thinking"}, 600.0),
        ({"mode": "pro", "plan": "pro", "timeout_s": 90.0}, 90.0),
        ({"mode": "thinking", "timeout_s": 5.0}, 5.0),
    ],
)
def test_timeout_defaults_reach_the_driver(ego, chrome, tmp_path, kwargs, expected):
    llm = ChatGPTWebLLM(browser="chrome", **kwargs)
    llm.complete("q")
    llm.generate_image("q", tmp_path / "out")
    assert [k["timeout_s"] for k in chrome.kwargs] == [expected, expected]


@pytest.mark.parametrize("mode", ["default", "thinking", "pro"])
def test_mode_forwarded_to_run_text_and_run_image(ego, chrome, tmp_path, mode):
    llm = ChatGPTWebLLM(browser="chrome", mode=mode, plan="pro")
    llm.complete("q")
    llm.generate_image("q", tmp_path / "out")
    assert [k["mode"] for k in chrome.kwargs] == [mode, mode]


def test_mode_reaches_the_ego_script(ego):
    ChatGPTWebLLM(browser="ego", mode="thinking").complete("q")
    line = next(l for l in (ego.dir / "script.js").read_text().splitlines() if l.startswith("const ARGS = "))
    assert json.loads(line[len("const ARGS = "):].rstrip(";"))["mode"] == "thinking"


def test_constructor_spawns_nothing(ego):
    ChatGPTWebLLM()
    assert not (ego.dir / "calls").exists()


def test_quota_before_keypress_never_touches_codex(ego, fake_bin, tmp_path):
    fake_bin("codex", '#!/bin/bash\ntouch "$STUB_DIR/codex_ran"\n')
    ego(fail("quota"), keypress=False, code=1)
    with pytest.raises(QuotaError):
        ChatGPTWebLLM().complete("q")
    assert not (ego.dir / "codex_ran").exists()
    assert os.environ["HOME"] == str(tmp_path)


def test_empty_prompt_is_client_error_before_any_ego_call(ego):
    with pytest.raises(ClientError, match="empty"):
        ChatGPTWebLLM().complete("  \n\t ")
    assert not (ego.dir / "calls").exists()


@pytest.fixture
def egoimg(fake_bin, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / "stub"
    d.mkdir()
    (d / "png.bin").write_bytes(PNG_BYTES)
    monkeypatch.setenv("STUB_DIR", str(d))
    fake_bin("ego-browser", IMG_STUB.replace("case \"$1\" in", 'echo "$@" >> "$STUB_DIR/calls"\ncase "$1" in', 1))
    monkeypatch.setenv("STUB_STDERR", "")
    monkeypatch.setenv("STUB_KEYPRESS", "1")
    monkeypatch.setenv("STUB_CODE", "0")
    monkeypatch.setenv("STUB_IMG_CMD", 'cp "$STUB_DIR/png.bin" "$idir/img-1"')
    monkeypatch.setenv("STUB_STDOUT", ev(phase="accepted", conversation_url=URL) + "\n" + img_ok("img-1"))
    d_out = tmp_path / "out"
    return d, d_out


def test_generate_image(egoimg):
    d, out = egoimg
    r = ChatGPTWebLLM().generate_image("a red cube", out)
    assert isinstance(r, ImageResult)
    assert r.route == "web" and r.conversation_url == URL and r.text == "here"
    assert [p.parent for p in r.paths] == [out.resolve()] and r.paths[0].read_bytes() == PNG_BYTES


def test_generate_image_prompt_carries_instruction(egoimg):
    d, out = egoimg
    ChatGPTWebLLM().generate_image("a red cube", out)
    line = next(l for l in (d / "script.js").read_text().splitlines() if l.startswith("const ARGS = "))
    args = json.loads(line[len("const ARGS = "):].rstrip(";"))
    assert args["prompt"] == "Generate an image: a red cube" and args["kind"] == "image"


def test_generate_image_unusable_out_dir_never_runs_the_browser(egoimg, tmp_path):
    d, _ = egoimg
    blocker = tmp_path / "file"
    blocker.write_text("x")
    for bad in (blocker, blocker / "sub"):
        with pytest.raises(ClientError, match="out_dir") as ei:
            ChatGPTWebLLM().generate_image("x", bad)
        assert type(ei.value) is ClientError
    assert not (d / "ran").exists() and not (d / "calls").exists()


def test_generate_image_region_guard_first(egoimg):
    d, out = egoimg
    guard = RegionGuard(allowed_regions={"US"}, lookup=lambda: "XX")
    with pytest.raises(RegionError):
        ChatGPTWebLLM(region_guard=guard).generate_image("x", out)
    assert not (d / "calls").exists() and not out.exists()


def test_generate_image_empty_prompt_before_any_subprocess(egoimg):
    d, out = egoimg
    with pytest.raises(ClientError, match="empty"):
        ChatGPTWebLLM().generate_image(" \n\t ", out)
    assert not (d / "calls").exists() and not out.exists()


def test_generate_image_is_one_attempt(egoimg, monkeypatch):
    d, out = egoimg
    monkeypatch.setenv("STUB_STDOUT", ev(phase="accepted", conversation_url=URL) + "\n" + fail("output", "no image in the answer", url=URL))
    monkeypatch.setenv("STUB_CODE", "1")
    with pytest.raises(OutputError):
        ChatGPTWebLLM().generate_image("x", out)
    assert (d / "calls").read_text().count("nodejs") == 2  # health check + one run


def test_is_image_generator():
    assert isinstance(ChatGPTWebLLM(), ImageGenerator)
