import json
import os

import pytest

from subllm import ChatGPTWebLLM, RegionGuard, WebTextResult
from subllm.base import ImageGenerator, ImageResult
from subllm.errors import ClientError, OutputError, QuotaError, RegionError

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


def test_chrome_not_available(ego):
    with pytest.raises(ClientError, match="Chrome"):
        ChatGPTWebLLM(browser="chrome").complete("q")
    assert not (ego.dir / "calls").exists()


@pytest.mark.parametrize("browser", ["auto", "ego"])
def test_ego_missing(browser, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(ClientError, match="not on PATH"):
        ChatGPTWebLLM(browser=browser).complete("q")


def test_auto_missing_mentions_chrome_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(ClientError, match="Chrome fallback"):
        ChatGPTWebLLM().complete("q")


@pytest.mark.parametrize(
    "kwargs",
    [{"mode": "thinking"}, {"plan": "gold"}, {"browser": "firefox"}],
)
def test_bad_args(kwargs):
    with pytest.raises(ValueError):
        ChatGPTWebLLM(**kwargs)


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
