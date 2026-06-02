import pytest
from subllm.base import BaseLLM, DryRunLLM, _retry
from subllm.errors import ClientError, OutputError, QuotaError


def test_retry_succeeds_after_transient_failures():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OutputError("bad json")
        return "ok"

    assert _retry(flaky, attempts=3, base_delay=0) == "ok"
    assert calls["n"] == 3


def test_retry_does_not_retry_quota():
    calls = {"n": 0}

    def quota():
        calls["n"] += 1
        raise QuotaError("cap")

    with pytest.raises(QuotaError):
        _retry(quota, attempts=3, base_delay=0)
    assert calls["n"] == 1  # QuotaError is not in retry_on -> raised immediately


def test_retry_reraises_after_exhausting_attempts():
    def always_bad():
        raise ClientError("crash")

    with pytest.raises(ClientError):
        _retry(always_bad, attempts=2, base_delay=0)


def test_dryrun_captures_prompts():
    llm = DryRunLLM()
    assert llm.complete("hello") == ""
    assert llm.complete_json("world") == {}
    assert llm.captured == ["hello", "world"]
