import json

import pytest

from subllm.cli import main

from .test_chatgpt_web_ego import URL, ev, fail
from .test_chatgpt_web_llm import _ok, ego  # noqa: F401


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
