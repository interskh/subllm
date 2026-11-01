import json

import pytest

from subllm.cli import main

from .test_chatgpt_web_ego import URL, ev, fail, res
from subllm.errors import HumanActionRequired

from .test_chatgpt_web_llm import _ok, chrome, ego  # noqa: F401


def test_complete_prints_text(ego, capsys):
    assert main(["complete", "--client", "chatgpt-web", "hi"]) == 0
    assert capsys.readouterr().out == "hello\n"


def test_complete_json_with_schema(ego, capsys):
    ego(_ok('{"a": 1}'))
    assert main(["complete-json", "--client", "chatgpt-web", "--browser", "ego", "q"]) == 0
    assert json.loads(capsys.readouterr().out) == {"a": 1}


def test_human_action_exits_3(ego, capsys):
    ego(fail("human_action", "log in to chatgpt.com in ego lite"), keypress=False, code=1)
    assert main(["complete", "--client", "chatgpt-web", "hi"]) == 3
    assert capsys.readouterr().err.startswith("error: HumanActionRequired:")


def test_quota_exits_1_typed(ego, capsys):
    ego(fail("quota"), keypress=False, code=1)
    assert main(["complete", "--client", "chatgpt-web", "hi"]) == 1
    assert capsys.readouterr().err.startswith("error: QuotaError:")


def test_send_unconfirmed_includes_url(ego, capsys):
    ego(ev(phase="accepted", conversation_url=URL), keypress=True, code=1)
    assert main(["complete", "--client", "chatgpt-web", "hi"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: SendUnconfirmedError:") and URL in err


@pytest.mark.parametrize("flag", [["--effort", "low"], ["--model", "m"]])
def test_effort_model_rejected(flag, capsys):
    assert main(["complete", "--client", "chatgpt-web", *flag, "hi"]) == 2
    assert "chatgpt-web" in capsys.readouterr().err


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_browser_rejected_for_other_clients(client, capsys):
    assert main(["complete", "--client", client, "--browser", "ego", "hi"]) == 2
    assert "--browser" in capsys.readouterr().err


def test_codex_default_effort_is_medium(monkeypatch, capsys):
    seen = {}

    class Fake:
        def __init__(self, model=None, reasoning_effort=None, **kw):
            seen["effort"] = reasoning_effort

        def complete(self, prompt):
            return "x"

    monkeypatch.setattr("subllm.codex.CodexLLM", Fake)
    assert main(["complete", "--client", "codex", "hi"]) == 0
    assert seen["effort"] == "medium"
    assert main(["complete", "--client", "codex", "--effort", "high", "hi"]) == 0
    assert seen["effort"] == "high"


def test_chrome_browser_prints_text(ego, chrome, capsys):
    assert main(["complete", "--client", "chatgpt-web", "--browser", "chrome", "hi"]) == 0
    assert capsys.readouterr().out == "from chrome\n"
    assert not (ego.dir / "calls").exists()


def test_chrome_human_action_exits_3(ego, chrome, capsys):
    chrome.run_error = HumanActionRequired("log in", browser="chrome")
    assert main(["complete", "--client", "chatgpt-web", "--browser", "chrome", "hi"]) == 3
    assert capsys.readouterr().err.startswith("error: HumanActionRequired:")


class _Spy:
    seen: list = []

    def __init__(self, **kw):
        _Spy.seen.append(kw)

    def complete(self, prompt):
        return "spy"

    def complete_json(self, prompt):
        return {"a": 1}

    def generate_image(self, prompt, out_dir):
        from subllm.base import ImageResult

        return ImageResult(paths=[], route="web", text="t", conversation_url=None, served_model=None)


@pytest.fixture
def spy(monkeypatch):
    from subllm import chatgpt_web as cw

    _Spy.seen = []
    monkeypatch.setattr(cw, "ChatGPTWebLLM", _Spy)
    for v in ("SUBLLM_CHATGPT_PLAN",):
        monkeypatch.delenv(v, raising=False)
    return _Spy


@pytest.mark.parametrize("cmd", ["complete", "complete-json"])
def test_mode_and_plan_reach_the_client(spy, cmd, capsys):
    assert main([cmd, "--client", "chatgpt-web", "--mode", "pro", "--plan", "pro", "hi"]) == 0
    assert spy.seen == [{"mode": "pro", "plan": "pro", "browser": "auto"}]


def test_defaults_are_default_mode_plus_plan(spy):
    assert main(["complete", "--client", "chatgpt-web", "hi"]) == 0
    assert spy.seen == [{"mode": "default", "plan": "plus", "browser": "auto"}]


def test_env_plan_used_when_flag_omitted(spy, monkeypatch):
    monkeypatch.setenv("SUBLLM_CHATGPT_PLAN", "pro")
    assert main(["complete", "--client", "chatgpt-web", "--mode", "pro", "hi"]) == 0
    assert spy.seen[0]["plan"] == "pro"


def test_plan_flag_beats_env(spy, monkeypatch):
    monkeypatch.setenv("SUBLLM_CHATGPT_PLAN", "pro")
    assert main(["complete", "--client", "chatgpt-web", "--plan", "free", "hi"]) == 0
    assert spy.seen[0]["plan"] == "free"


def test_bad_env_plan_is_rc2_naming_the_variable(spy, monkeypatch, capsys):
    monkeypatch.setenv("SUBLLM_CHATGPT_PLAN", "gold")
    assert main(["complete", "--client", "chatgpt-web", "hi"]) == 2
    assert "SUBLLM_CHATGPT_PLAN" in capsys.readouterr().err
    assert spy.seen == []


def test_bad_env_plan_ignored_when_flag_given(spy, monkeypatch):
    monkeypatch.setenv("SUBLLM_CHATGPT_PLAN", "gold")
    assert main(["complete", "--client", "chatgpt-web", "--plan", "plus", "hi"]) == 0


def test_pro_mode_without_pro_plan_is_rc2_and_sends_nothing(ego, capsys):
    assert main(["complete", "--client", "chatgpt-web", "--mode", "pro", "hi"]) == 2
    err = capsys.readouterr().err
    assert "needs plan='pro'" in err and "Traceback" not in err
    assert not (ego.dir / "calls").exists()


def test_pro_mode_with_pro_plan_reaches_ego_script(ego, capsys):
    assert main(["complete", "--client", "chatgpt-web", "--mode", "pro", "--plan", "pro", "hi"]) == 0
    line = next(l for l in (ego.dir / "script.js").read_text().splitlines() if l.startswith("const ARGS = "))
    args = json.loads(line[len("const ARGS = "):].rstrip(";"))
    assert args["mode"] == "pro" and args["timeout_ms"] == 2700 * 1000


@pytest.mark.parametrize("client", ["codex", "claude"])
@pytest.mark.parametrize("flag", [["--mode", "thinking"], ["--plan", "pro"]])
def test_mode_plan_rejected_for_other_clients(client, flag, capsys):
    assert main(["complete", "--client", client, *flag, "hi"]) == 2
    assert flag[0] in capsys.readouterr().err


def test_env_plan_never_errors_for_codex(monkeypatch, capsys):
    monkeypatch.setenv("SUBLLM_CHATGPT_PLAN", "gold")

    class Fake:
        def __init__(self, **kw):
            pass

        def complete(self, prompt):
            return "x"

    monkeypatch.setattr("subllm.codex.CodexLLM", Fake)
    assert main(["complete", "--client", "codex", "hi"]) == 0


def test_model_downgrade_prints_typed_error(ego, monkeypatch, capsys):
    ego(res(ok=False, kind="downgrade", detail="requested pro, served m", conversation_url=URL, text="ans",
            served_model="m", requested_mode="pro"), keypress=True, code=1)
    assert main(["complete", "--client", "chatgpt-web", "--mode", "pro", "--plan", "pro", "hi"]) == 1
    assert capsys.readouterr().err.startswith("error: ModelDowngradeError:")
