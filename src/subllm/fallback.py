"""FallbackLLM — explicit, opt-in resilience across clients.

Tries clients in order; on an exception whose type is in `on`, advances to the
next. Any other exception propagates immediately (fail loud). Re-raises the last
caught error if every client is exhausted.
"""
from __future__ import annotations

from typing import Any

from subllm.base import BaseLLM
from subllm.errors import QuotaError


class FallbackLLM(BaseLLM):
    def __init__(
        self,
        primary: BaseLLM,
        *fallbacks: BaseLLM,
        on: tuple[type[Exception], ...] = (QuotaError,),
    ) -> None:
        self._clients = (primary, *fallbacks)
        self._on = on

    def _dispatch(self, method: str, *args: Any) -> Any:
        last: Exception | None = None
        for client in self._clients:
            try:
                return getattr(client, method)(*args)
            except self._on as e:
                last = e
                continue
        assert last is not None
        raise last

    def complete(self, prompt: str) -> str:
        return self._dispatch("complete", prompt)

    def complete_json(self, prompt: str) -> dict[str, Any]:
        return self._dispatch("complete_json", prompt)

    def complete_json_schema(self, prompt: str, schema_model: type) -> dict[str, Any]:
        return self._dispatch("complete_json_schema", prompt, schema_model)
