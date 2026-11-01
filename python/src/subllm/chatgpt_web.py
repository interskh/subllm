"""ChatGPTWebLLM — the chatgpt.com web app in a real logged-in browser."""
from __future__ import annotations

from typing import Any

from subllm.base import BaseLLM, WebTextResult
from subllm.codex import _JSON_INSTRUCTION, _parse_json_object
from subllm.drivers.chatgpt_web.ego import EgoDriver
from subllm.errors import ClientError, OutputError
from subllm.preflight import RegionGuard

_PLANS = ("free", "plus", "pro")
_BROWSERS = ("auto", "ego", "chrome")
_DEFAULT_TIMEOUT_S = 600.0


class ChatGPTWebLLM(BaseLLM):
    def __init__(
        self,
        mode: str = "default",
        plan: str = "plus",
        browser: str = "auto",
        region_guard: RegionGuard | None = None,
        timeout_s: float | None = None,
    ) -> None:
        if mode != "default":
            raise ValueError(
                f"unsupported mode {mode!r}: only 'default' is available; other modes arrive in a later release"
            )
        if plan not in _PLANS:
            raise ValueError(f"plan must be one of {_PLANS}, got {plan!r}")
        if browser not in _BROWSERS:
            raise ValueError(f"browser must be one of {_BROWSERS}, got {browser!r}")
        self._mode = mode
        self._plan = plan
        self._browser = browser
        self._guard = region_guard
        self._timeout_s = _DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s
        self._driver: EgoDriver | None = None

    def _resolve_driver(self) -> EgoDriver:
        if self._driver is not None:
            return self._driver
        if self._browser == "chrome":
            raise ClientError("the Chrome driver is not available yet")
        driver = EgoDriver()
        ok, reason = driver.available()
        if not ok:
            if self._browser == "ego":
                raise ClientError(f"ego lite is not available: {reason}")
            raise ClientError(
                f"ego lite is not available: {reason}; the Chrome fallback is not available yet"
            )
        self._driver = driver
        return driver

    def complete_result(self, prompt: str) -> WebTextResult:
        if self._guard is not None:
            self._guard.check()  # RegionError before any browser subprocess
        driver = self._resolve_driver()
        driver.check_guide()
        result = driver.run_text(prompt, timeout_s=self._timeout_s)
        if not result.text.strip():
            raise OutputError("chatgpt.com returned an empty answer")
        return result

    def complete(self, prompt: str) -> str:
        return self.complete_result(prompt).text

    def complete_json(self, prompt: str) -> dict[str, Any]:
        return _parse_json_object(self.complete(prompt + _JSON_INSTRUCTION))
