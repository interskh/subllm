"""BaseLLM contract, transient-retry helper, and a no-op DryRunLLM."""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Callable, TypeVar

from subllm.errors import ClientError, OutputError

T = TypeVar("T")

# Default transient failures worth retrying within a single client. QuotaError
# and RegionError are intentionally excluded — retrying them is pointless.
DEFAULT_RETRY_ON: tuple[type[Exception], ...] = (ClientError, OutputError)


def _retry(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    retry_on: tuple[type[Exception], ...] = DEFAULT_RETRY_ON,
) -> T:
    """Exponential backoff (base, 2*base, 4*base ...). Retries only `retry_on`
    types; anything else propagates immediately. Re-raises the last error after
    `attempts` tries."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except retry_on as e:
            last = e
            if i < attempts - 1 and base_delay:
                time.sleep(base_delay * (2 ** i))
        # non-retryable exceptions are not caught -> propagate
    assert last is not None
    raise last


class BaseLLM(ABC):
    @abstractmethod
    def complete(self, prompt: str) -> str: ...

    @abstractmethod
    def complete_json(self, prompt: str) -> dict[str, Any]: ...

    def complete_json_schema(self, prompt: str, schema_model: type) -> dict[str, Any]:
        """Default: ignore the schema, route through complete_json. Clients with
        native structured output (codex --output-schema) override this."""
        return self.complete_json(prompt)


class DryRunLLM(BaseLLM):
    """No-op LLM: records prompts it would have sent. For consumers' tests."""

    def __init__(self) -> None:
        self.captured: list[str] = []

    def complete(self, prompt: str) -> str:
        self.captured.append(prompt)
        return ""

    def complete_json(self, prompt: str) -> dict[str, Any]:
        self.captured.append(prompt)
        return {}


def _validate_with_model(parsed: dict[str, Any], schema_model: type) -> dict[str, Any]:
    """Validate a parsed dict against a Pydantic model; raise OutputError on
    mismatch. Shared by CodexLLM and ClaudeLLM so structured output is actually
    enforced, not merely requested."""
    from pydantic import ValidationError

    try:
        return schema_model.model_validate(parsed).model_dump()
    except ValidationError as e:
        raise OutputError(f"output failed schema validation: {e}") from e
