import pytest
from subllm.fallback import FallbackLLM
from subllm.base import BaseLLM
from subllm.errors import QuotaError, OutputError


class _Stub(BaseLLM):
    def __init__(self, *, raises=None, text="ok"):
        self._raises = raises
        self._text = text
        self.calls = 0

    def complete(self, prompt):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._text

    def complete_json(self, prompt):
        self.calls += 1
        if self._raises:
            raise self._raises
        return {"ok": True}


def test_uses_primary_when_it_succeeds():
    primary = _Stub(text="primary")
    backup = _Stub(text="backup")
    llm = FallbackLLM(primary, backup)
    assert llm.complete("x") == "primary"
    assert backup.calls == 0


def test_falls_back_on_quota_error():
    primary = _Stub(raises=QuotaError("cap"))
    backup = _Stub(text="backup")
    llm = FallbackLLM(primary, backup)
    assert llm.complete("x") == "backup"
    assert primary.calls == 1 and backup.calls == 1


def test_does_not_fall_back_on_output_error():
    # OutputError is a real bug, not a budget problem -> propagate, do not retry elsewhere
    primary = _Stub(raises=OutputError("bad json"))
    backup = _Stub(text="backup")
    llm = FallbackLLM(primary, backup)
    with pytest.raises(OutputError):
        llm.complete("x")
    assert backup.calls == 0


def test_reraises_last_when_all_fail():
    a = _Stub(raises=QuotaError("a"))
    b = _Stub(raises=QuotaError("b"))
    with pytest.raises(QuotaError):
        FallbackLLM(a, b).complete("x")
