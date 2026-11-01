"""ChatGPTWebLLM — the chatgpt.com web app in a real logged-in browser."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from subllm._images import prepare_out_dir
from subllm.base import BaseLLM, ImageGenerator, ImageResult, WebTextResult
from subllm.codex import _JSON_INSTRUCTION, _parse_json_object
from subllm.drivers.chatgpt_web.chrome import ChromeDriver
from subllm.drivers.chatgpt_web.ego import EgoDriver, normalize_prompt
from subllm.errors import ClientError, OutputError
from subllm.preflight import RegionGuard

_PLANS = ("free", "plus", "pro")
_BROWSERS = ("auto", "ego", "chrome")
_MODES = ("default", "thinking", "pro")
_DEFAULT_TIMEOUT_S = 600.0
_PRO_TIMEOUT_S = 2700.0
_IMAGE_INSTRUCTION = "Generate an image: "


class ChatGPTWebLLM(BaseLLM, ImageGenerator):
    def __init__(
        self,
        mode: str = "default",
        plan: str = "plus",
        browser: str = "auto",
        region_guard: RegionGuard | None = None,
        timeout_s: float | None = None,
    ) -> None:
        if mode not in _MODES:
            raise ValueError(f"mode must be one of {_MODES}, got {mode!r}")
        if plan not in _PLANS:
            raise ValueError(f"plan must be one of {_PLANS}, got {plan!r}")
        if browser not in _BROWSERS:
            raise ValueError(f"browser must be one of {_BROWSERS}, got {browser!r}")
        if mode == "pro" and plan != "pro":
            raise ValueError(f"mode 'pro' needs plan='pro' (got {plan!r})")
        self._mode = mode
        self._plan = plan
        self._browser = browser
        self._guard = region_guard
        if timeout_s is None:
            timeout_s = _PRO_TIMEOUT_S if mode == "pro" else _DEFAULT_TIMEOUT_S
        self._timeout_s = timeout_s
        self._driver: EgoDriver | ChromeDriver | None = None

    def _resolve_driver(self) -> EgoDriver | ChromeDriver:
        if self._driver is not None:
            return self._driver
        driver: EgoDriver | ChromeDriver
        if self._browser == "chrome":
            driver = ChromeDriver()
            ok, reason = driver.available()
            if not ok:
                raise ClientError(f"Chrome is not available: {reason}")
        else:
            driver = EgoDriver()
            ok, reason = driver.available()
            if not ok:
                if self._browser == "ego":
                    raise ClientError(f"ego lite is not available: {reason}")
                driver = ChromeDriver()
                chrome_ok, chrome_reason = driver.available()
                if not chrome_ok:
                    raise ClientError(
                        f"no browser is available: ego lite is not available: {reason}; "
                        f"Chrome is not available: {chrome_reason}"
                    )
        self._driver = driver
        return driver

    def complete_result(self, prompt: str) -> WebTextResult:
        if self._guard is not None:
            self._guard.check()  # RegionError before any browser subprocess
        if not normalize_prompt(prompt).strip():
            raise ClientError("the prompt is empty")
        driver = self._resolve_driver()
        driver.check_guide()
        result = driver.run_text(prompt, timeout_s=self._timeout_s, mode=self._mode)
        if not result.text.strip():
            raise OutputError("chatgpt.com returned an empty answer")
        return result

    def generate_image(self, prompt: str, out_dir: str | Path) -> ImageResult:
        if self._guard is not None:
            self._guard.check()  # RegionError before any browser subprocess
        if not normalize_prompt(prompt).strip():
            raise ClientError("the prompt is empty")
        dest = prepare_out_dir(out_dir)
        driver = self._resolve_driver()
        driver.check_guide()
        return driver.run_image(
            _IMAGE_INSTRUCTION + prompt, dest, timeout_s=self._timeout_s, mode=self._mode
        )

    def complete(self, prompt: str) -> str:
        return self.complete_result(prompt).text

    def complete_json(self, prompt: str) -> dict[str, Any]:
        return _parse_json_object(self.complete(prompt + _JSON_INSTRUCTION))
