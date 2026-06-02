import pytest
from subllm.errors import SubllmError, QuotaError, ClientError, OutputError, RegionError


def test_all_errors_subclass_base():
    for cls in (QuotaError, ClientError, OutputError, RegionError):
        assert issubclass(cls, SubllmError)


def test_errors_carry_message():
    err = QuotaError("codex 5h cap reached")
    assert "5h cap" in str(err)
