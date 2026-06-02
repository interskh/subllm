# subllm Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python library that runs LLM requests (summarize / search-and-summarize) through flat-rate coding subscriptions via the genuine official CLIs (`codex exec`, interactive `claude` over tmux) — drop-in for a `BaseLLM` seam, no per-token API, no proxy.

**Architecture:** A `BaseLLM` ABC with three methods (`complete`, `complete_json`, `complete_json_schema`). Two concrete clients — `CodexLLM` (primary, wraps `codex exec` in an isolated `CODEX_HOME`) and `ClaudeLLM` (drives interactive `claude` in an ephemeral tmux pane, detecting turn completion by tailing the JSONL transcript). An explicit `FallbackLLM` composite, an optional `RegionGuard` preflight, and a minimal CLI. Subprocess invocation lives in an isolated `drivers/` layer so a future TS/Go port reimplements only that.

**Tech Stack:** Python 3.12, `uv` (venv + deps), `pydantic` (schema models), `pytest`. Stdlib `subprocess` for client invocation. Tests use stub `codex`/`tmux` executables on `PATH` — never real subscription calls.

**Read before starting:** `docs/design.md` (authoritative spec), `docs/research/research_subscription-summarization_2026-06-02.md` (gotchas: codex context pollution, claude 2026-06-15 billing split, JSONL completion detection). The claude tmux driver is adapted from `~/Projects/ralph-loop/ralph_lib/drivers.py` — reference it for plumbing, but strip ralph's bash-watchdog event emission.

**Conventions:**
- `uv run pytest ...` for all test runs.
- Commit after every task (frequent commits).
- No real `codex`/`tmux`/network calls in tests.
- Match names exactly across tasks: methods `complete`/`complete_json`/`complete_json_schema`; errors `SubllmError`/`QuotaError`/`ClientError`/`OutputError`/`RegionError`.

---

## Phase 1 — Foundation (independently testable)

### Task 1: Project skeleton + uv setup

**Files:**
- Create: `pyproject.toml`
- Create: `src/subllm/__init__.py`
- Create: `tests/__init__.py`
- Create: `.gitignore`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "subllm"
version = "0.1.0"
description = "Run LLM requests through flat-rate coding subscriptions via native official clients."
requires-python = ">=3.12"
dependencies = ["pydantic>=2.6"]

[project.scripts]
subllm = "subllm.cli:main"

[dependency-groups]
dev = ["pytest>=8"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/subllm"]
```

- [ ] **Step 2: Create empty package + test init**

`src/subllm/__init__.py`:
```python
"""subllm — subscription-native LLM clients."""
```

`tests/__init__.py`: (empty file)

- [ ] **Step 3: Write `.gitignore`**

```
.venv/
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
dist/
```

- [ ] **Step 4: Sync the environment**

Run: `cd ~/Projects/subllm && uv sync`
Expected: creates `.venv`, installs pydantic + pytest, prints "Resolved ... packages".

- [ ] **Step 5: Verify pytest runs (no tests yet)**

Run: `uv run pytest -q`
Expected: "no tests ran" (exit 5) — acceptable; confirms pytest is wired.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/subllm/__init__.py tests/__init__.py .gitignore uv.lock
git commit -m "chore: project skeleton + uv setup"
```

---

### Task 2: Error types

**Files:**
- Create: `src/subllm/errors.py`
- Test: `tests/test_errors.py`

- [ ] **Step 1: Write the failing test**

`tests/test_errors.py`:
```python
import pytest
from subllm.errors import SubllmError, QuotaError, ClientError, OutputError, RegionError


def test_all_errors_subclass_base():
    for cls in (QuotaError, ClientError, OutputError, RegionError):
        assert issubclass(cls, SubllmError)


def test_errors_carry_message():
    err = QuotaError("codex 5h cap reached")
    assert "5h cap" in str(err)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_errors.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'subllm.errors'`.

- [ ] **Step 3: Write minimal implementation**

`src/subllm/errors.py`:
```python
"""subllm exception hierarchy. All errors subclass SubllmError."""


class SubllmError(Exception):
    """Base for every error raised by subllm."""


class QuotaError(SubllmError):
    """Subscription quota exhausted (codex 5h/weekly cap, claude credit/limit).

    The only default trigger for FallbackLLM.
    """


class ClientError(SubllmError):
    """Subprocess failed: missing binary, crash, auth missing/invalid, tmux failure."""


class OutputError(SubllmError):
    """Model output unusable: empty, invalid JSON, or schema-validation mismatch."""


class RegionError(SubllmError):
    """Preflight: public IP outside allowed regions (or lookup failed under block).

    Hard stop; NOT a default fallback trigger — every subscription client is
    equally out-of-region.
    """
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_errors.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/subllm/errors.py tests/test_errors.py
git commit -m "feat: error hierarchy"
```

---

### Task 3: BaseLLM ABC + retry + DryRunLLM

**Files:**
- Create: `src/subllm/base.py`
- Test: `tests/test_base.py`

- [ ] **Step 1: Write the failing test**

`tests/test_base.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_base.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'subllm.base'`.

- [ ] **Step 3: Write minimal implementation**

`src/subllm/base.py`:
```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_base.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/subllm/base.py tests/test_base.py
git commit -m "feat: BaseLLM ABC + transient retry + DryRunLLM"
```

---

### Task 4: RegionGuard preflight

**Files:**
- Create: `src/subllm/preflight.py`
- Test: `tests/test_preflight.py`

- [ ] **Step 1: Write the failing test**

`tests/test_preflight.py`:
```python
import pytest
from subllm.preflight import RegionGuard
from subllm.errors import RegionError


def test_in_region_passes():
    guard = RegionGuard(allowed_regions={"US", "JP"}, lookup=lambda: "US", ttl_s=0)
    guard.check()  # no raise


def test_out_of_region_raises_and_names_country():
    guard = RegionGuard(allowed_regions={"US"}, lookup=lambda: "CN", ttl_s=0)
    with pytest.raises(RegionError) as ei:
        guard.check()
    assert "CN" in str(ei.value)


def test_lookup_failure_blocks_by_default():
    def boom():
        raise OSError("network down")

    guard = RegionGuard(allowed_regions={"US"}, lookup=boom, ttl_s=0)
    with pytest.raises(RegionError):
        guard.check()


def test_lookup_failure_can_allow():
    def boom():
        raise OSError("network down")

    guard = RegionGuard(
        allowed_regions={"US"}, lookup=boom, ttl_s=0, on_lookup_failure="allow"
    )
    guard.check()  # no raise


def test_result_is_cached_within_ttl():
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return "US"

    guard = RegionGuard(allowed_regions={"US"}, lookup=counting, ttl_s=999)
    guard.check()
    guard.check()
    assert calls["n"] == 1  # second check served from cache
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_preflight.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'subllm.preflight'`.

- [ ] **Step 3: Write minimal implementation**

`src/subllm/preflight.py`:
```python
"""Optional region/IP preflight. Off unless a client is given a RegionGuard.

Guards against VPN-off -> out-of-region requests that could fail or risk the
subscription account.
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Callable

from subllm.errors import RegionError


def default_lookup(timeout_s: float = 5.0) -> str:
    """Resolve the current public IP's ISO country code via a small geo-IP HTTP
    service. Injectable so tests/consumers can replace it."""
    with urllib.request.urlopen("https://ipinfo.io/json", timeout=timeout_s) as resp:
        data = json.loads(resp.read().decode())
    country = data.get("country")
    if not country:
        raise RuntimeError("geo-IP lookup returned no country")
    return country


class RegionGuard:
    def __init__(
        self,
        allowed_regions: set[str],
        lookup: Callable[[], str] = default_lookup,
        ttl_s: float = 300.0,
        on_lookup_failure: str = "block",  # "block" | "allow"
    ) -> None:
        if on_lookup_failure not in ("block", "allow"):
            raise ValueError("on_lookup_failure must be 'block' or 'allow'")
        self._allowed = {r.upper() for r in allowed_regions}
        self._lookup = lookup
        self._ttl_s = ttl_s
        self._on_lookup_failure = on_lookup_failure
        self._cached_country: str | None = None
        self._cached_at: float = 0.0

    def _resolve_country(self) -> str:
        now = time.monotonic()
        if self._cached_country is not None and (now - self._cached_at) < self._ttl_s:
            return self._cached_country
        country = self._lookup().upper()
        self._cached_country = country
        self._cached_at = now
        return country

    def check(self) -> None:
        """Raise RegionError if the public IP is outside allowed_regions, or if
        the lookup fails under on_lookup_failure='block'. No-op when in region."""
        try:
            country = self._resolve_country()
        except Exception as e:  # noqa: BLE001 — lookup failure policy applies
            if self._on_lookup_failure == "allow":
                return
            raise RegionError(f"region lookup failed ({e}); blocking call") from e
        if country not in self._allowed:
            raise RegionError(
                f"public IP in {country}; expected one of "
                f"{sorted(self._allowed)} — is your VPN connected?"
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_preflight.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/subllm/preflight.py tests/test_preflight.py
git commit -m "feat: optional RegionGuard preflight"
```

---

## Phase 2 — Codex client (ship first)

### Task 5: codex_exec driver

**Files:**
- Create: `src/subllm/drivers/__init__.py`
- Create: `src/subllm/drivers/codex_exec.py`
- Test: `tests/conftest.py`
- Test: `tests/test_codex_exec.py`

- [ ] **Step 1: Write a stub-binary fixture**

`tests/conftest.py`:
```python
import os
import stat
from pathlib import Path

import pytest


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    """Create fake executables on PATH. Usage:
        fake_bin("codex", '#!/bin/bash\\necho hi')
    Returns the bin dir."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def _make(name: str, script: str) -> Path:
        p = bindir / name
        p.write_text(script)
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return p

    return _make
```

- [ ] **Step 2: Write the failing test**

`tests/test_codex_exec.py`:
```python
import json
import pytest
from subllm.drivers.codex_exec import run_codex_exec
from subllm.errors import QuotaError, ClientError


def test_returns_last_message(fake_bin, tmp_path):
    # codex writes the final message to the path given after -o
    fake_bin("codex", r'''#!/bin/bash
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; shift; fi
  shift
done
printf 'a tidy summary' > "$out"
''')
    result = run_codex_exec("summarize this", codex_home=str(tmp_path))
    assert result == "a tidy summary"


def test_passes_output_schema_when_given(fake_bin, tmp_path):
    # codex echoes its own argv to a sidecar so the test can inspect flags
    argv_log = tmp_path / "argv.txt"
    fake_bin("codex", rf'''#!/bin/bash
echo "$@" > "{argv_log}"
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; shift; fi
  shift
done
printf '{{"ok": true}}' > "$out"
''')
    schema_file = tmp_path / "s.json"
    schema_file.write_text('{"type": "object"}')
    run_codex_exec("x", codex_home=str(tmp_path), schema_path=str(schema_file))
    argv = argv_log.read_text()
    assert "--output-schema" in argv
    assert str(schema_file) in argv


def test_quota_message_maps_to_quota_error(fake_bin, tmp_path):
    fake_bin("codex", r'''#!/bin/bash
echo "You have hit your usage limit. Try again later." >&2
exit 1
''')
    with pytest.raises(QuotaError):
        run_codex_exec("x", codex_home=str(tmp_path))


def test_missing_binary_maps_to_client_error(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))  # no codex here
    with pytest.raises(ClientError):
        run_codex_exec("x", codex_home=str(tmp_path))
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_codex_exec.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'subllm.drivers'`.

- [ ] **Step 4: Write minimal implementation**

`src/subllm/drivers/__init__.py`:
```python
"""Subprocess drivers — the isolated, language-portable boundary.

A future TS/Go port reimplements only this package. See docs/drivers-contract.md.
"""
```

`src/subllm/drivers/codex_exec.py`:
```python
"""Invoke `codex exec` (ChatGPT-subscription auth) as a clean text/JSON engine.

Runs read-only and config-isolated to avoid the AGENTS.md context-pollution
gotcha documented in docs/research.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from subllm.errors import ClientError, QuotaError

# stderr substrings that mean "out of subscription budget", not a bug.
_QUOTA_PATTERNS = re.compile(
    r"usage limit|rate.?limit|quota|too many requests|try again later",
    re.IGNORECASE,
)


def run_codex_exec(
    prompt: str,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    search: bool = False,
    codex_home: str | None = None,
    schema_path: str | None = None,
    timeout_s: int = 120,
) -> str:
    """Run one non-interactive codex turn. Returns the final message text.

    Raises QuotaError on subscription-limit stderr, ClientError otherwise.
    """
    with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False) as out_f:
        out_path = out_f.name
    try:
        argv = [
            "codex", "exec",
            "-s", "read-only",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "-o", out_path,
        ]
        if model:
            argv += ["-m", model]
        if reasoning_effort:
            argv += ["-c", f'model_reasoning_effort="{reasoning_effort}"']
        if search:
            argv += ["--search"]
        if schema_path:
            argv += ["--output-schema", schema_path]
        argv += ["--", prompt]  # -- so a prompt starting with '-' isn't parsed as a flag

        env = dict(os.environ)
        if codex_home:
            env["CODEX_HOME"] = codex_home

        try:
            proc = subprocess.run(
                argv, env=env, capture_output=True, text=True, timeout=timeout_s
            )
        except FileNotFoundError as e:
            raise ClientError("codex binary not found on PATH") from e
        except subprocess.TimeoutExpired as e:
            raise ClientError(f"codex exec timed out after {timeout_s}s") from e

        if proc.returncode != 0:
            stderr = proc.stderr or ""
            if _QUOTA_PATTERNS.search(stderr):
                raise QuotaError(f"codex subscription limit: {stderr.strip()[:200]}")
            raise ClientError(
                f"codex exec failed (exit {proc.returncode}): {stderr.strip()[:200]}"
            )

        return Path(out_path).read_text()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_codex_exec.py -v`
Expected: PASS (4 passed).

- [ ] **Step 6: Commit**

```bash
git add src/subllm/drivers/__init__.py src/subllm/drivers/codex_exec.py tests/conftest.py tests/test_codex_exec.py
git commit -m "feat: codex exec driver"
```

---

### Task 6: CodexLLM

**Files:**
- Create: `src/subllm/codex.py`
- Modify: `src/subllm/__init__.py`
- Test: `tests/test_codex_llm.py`

- [ ] **Step 1: Write the failing test**

`tests/test_codex_llm.py`:
```python
import json
import pytest
from pydantic import BaseModel
from subllm.codex import CodexLLM
from subllm.errors import OutputError, RegionError


class _Summary(BaseModel):
    topic: str


def test_complete_returns_text(fake_bin, tmp_path):
    fake_bin("codex", r'''#!/bin/bash
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
printf 'hello world' > "$out"
''')
    llm = CodexLLM(model="gpt-5.4", codex_home=str(tmp_path))
    assert llm.complete("hi") == "hello world"


def test_complete_json_parses_object(fake_bin, tmp_path):
    fake_bin("codex", r'''#!/bin/bash
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
printf '{"topic": "weekend trip"}' > "$out"
''')
    llm = CodexLLM(model="gpt-5.4", codex_home=str(tmp_path))
    assert llm.complete_json("x") == {"topic": "weekend trip"}


def test_complete_json_rejects_non_object(fake_bin, tmp_path):
    fake_bin("codex", r'''#!/bin/bash
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
printf '[]' > "$out"
''')
    llm = CodexLLM(model="gpt-5.4", codex_home=str(tmp_path), attempts=1)
    with pytest.raises(OutputError):
        llm.complete_json("x")


def test_complete_json_schema_validates(fake_bin, tmp_path):
    fake_bin("codex", r'''#!/bin/bash
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
printf '{"topic": "ok"}' > "$out"
''')
    llm = CodexLLM(model="gpt-5.4", codex_home=str(tmp_path))
    assert llm.complete_json_schema("x", _Summary) == {"topic": "ok"}


def test_region_guard_blocks_before_calling(tmp_path):
    from subllm.preflight import RegionGuard
    guard = RegionGuard(allowed_regions={"US"}, lookup=lambda: "CN", ttl_s=0)
    # No fake codex on PATH; if the guard works, codex is never invoked.
    llm = CodexLLM(model="gpt-5.4", codex_home=str(tmp_path), region_guard=guard)
    with pytest.raises(RegionError):
        llm.complete("x")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_codex_llm.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'subllm.codex'`.

- [ ] **Step 3: Write minimal implementation**

`src/subllm/codex.py`:
```python
"""CodexLLM — primary client. Wraps `codex exec` behind the BaseLLM contract."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from subllm.base import BaseLLM, _retry
from subllm.drivers.codex_exec import run_codex_exec
from subllm.errors import OutputError
from subllm.preflight import RegionGuard

_JSON_INSTRUCTION = "\n\nReturn ONLY a single JSON object. No prose, no code fence."


class CodexLLM(BaseLLM):
    def __init__(
        self,
        model: str | None = None,
        reasoning_effort: str = "medium",
        search: bool = False,
        codex_home: str | None = None,
        region_guard: RegionGuard | None = None,
        attempts: int = 3,
        timeout_s: int = 120,
    ) -> None:
        self._model = model
        self._effort = reasoning_effort
        self._search = search
        self._codex_home = codex_home
        self._guard = region_guard
        self._attempts = attempts
        self._timeout_s = timeout_s

    def _run(self, prompt: str, schema_path: str | None = None) -> str:
        if self._guard is not None:
            self._guard.check()  # RegionError before any subprocess
        return run_codex_exec(
            prompt,
            model=self._model,
            reasoning_effort=self._effort,
            search=self._search,
            codex_home=self._codex_home,
            schema_path=schema_path,
            timeout_s=self._timeout_s,
        )

    def complete(self, prompt: str) -> str:
        return _retry(lambda: self._run(prompt), attempts=self._attempts)

    def complete_json(self, prompt: str) -> dict[str, Any]:
        def _call() -> dict[str, Any]:
            return _parse_json_object(self._run(prompt + _JSON_INSTRUCTION))
        return _retry(_call, attempts=self._attempts)

    def complete_json_schema(self, prompt: str, schema_model: type) -> dict[str, Any]:
        schema = json.dumps(schema_model.model_json_schema())
        def _call() -> dict[str, Any]:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False
            ) as f:
                f.write(schema)
                schema_path = f.name
            try:
                return _parse_json_object(self._run(prompt, schema_path=schema_path))
            finally:
                Path(schema_path).unlink(missing_ok=True)
        return _retry(_call, attempts=self._attempts)


def _parse_json_object(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        raise OutputError(f"codex returned invalid JSON: {raw[:120]!r}") from e
    if not isinstance(parsed, dict) or not parsed:
        raise OutputError(
            f"expected non-empty JSON object, got {type(parsed).__name__}"
        )
    return parsed
```

- [ ] **Step 4: Update package exports**

Replace `src/subllm/__init__.py` with:
```python
"""subllm — subscription-native LLM clients."""
from subllm.base import BaseLLM, DryRunLLM
from subllm.codex import CodexLLM
from subllm.errors import (
    ClientError,
    OutputError,
    QuotaError,
    RegionError,
    SubllmError,
)
from subllm.preflight import RegionGuard

__all__ = [
    "BaseLLM",
    "DryRunLLM",
    "CodexLLM",
    "RegionGuard",
    "SubllmError",
    "QuotaError",
    "ClientError",
    "OutputError",
    "RegionError",
]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_codex_llm.py -v`
Expected: PASS (5 passed).

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -q`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add src/subllm/codex.py src/subllm/__init__.py tests/test_codex_llm.py
git commit -m "feat: CodexLLM client"
```

---

### Task 7: Minimal CLI

**Files:**
- Create: `src/subllm/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

`tests/test_cli.py`:
```python
import json
import pytest
from subllm.cli import main


def test_complete_prints_text(fake_bin, tmp_path, capsys, monkeypatch):
    fake_bin("codex", r'''#!/bin/bash
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
printf 'cli summary' > "$out"
''')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    rc = main(["complete", "--client", "codex", "summarize this"])
    assert rc == 0
    assert "cli summary" in capsys.readouterr().out


def test_unknown_client_errors(capsys):
    rc = main(["complete", "--client", "bogus", "x"])
    assert rc != 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'subllm.cli'`.

- [ ] **Step 3: Write minimal implementation**

`src/subllm/cli.py`:
```python
"""Minimal CLI for smoke-testing both clients (and shell-out from non-Python).

    subllm complete       --client codex|claude [--model M] [--effort E] [PROMPT]
    subllm complete-json  --client codex|claude [--schema schema.json] [PROMPT]

PROMPT is read from the argument, or stdin when omitted / '-'.
"""
from __future__ import annotations

import argparse
import json
import sys

from subllm.errors import SubllmError


def _read_prompt(arg: str | None) -> str:
    if arg and arg != "-":
        return arg
    return sys.stdin.read()


def _build_client(name: str, args: argparse.Namespace):
    if name == "codex":
        from subllm.codex import CodexLLM
        return CodexLLM(model=args.model, reasoning_effort=args.effort)
    if name == "claude":
        from subllm.claude import ClaudeLLM
        return ClaudeLLM(model=args.model)
    raise SystemExit(f"unknown client: {name!r} (expected codex|claude)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="subllm")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for cmd in ("complete", "complete-json"):
        p = sub.add_parser(cmd)
        p.add_argument("--client", required=True, choices=["codex", "claude"])
        p.add_argument("--model", default=None)
        p.add_argument("--effort", default="medium")
        p.add_argument("--schema", default=None, help="JSON-schema file (complete-json)")
        p.add_argument("prompt", nargs="?", default=None)

    args = parser.parse_args(argv)
    try:
        client = _build_client(args.client, args)
        prompt = _read_prompt(args.prompt)
        if args.cmd == "complete":
            sys.stdout.write(client.complete(prompt))
        else:
            if args.schema:
                schema_dict = json.loads(open(args.schema).read())
                result = client.complete_json(
                    prompt + "\n\nMatch this JSON schema: " + json.dumps(schema_dict)
                )
            else:
                result = client.complete_json(prompt)
            sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
        return 0
    except SubllmError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1
    except SystemExit as e:
        sys.stderr.write(f"{e}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

Note: the CLI's `complete-json --schema` path feeds the schema as prompt text
(works for both clients). The native `--output-schema` binding is exercised via
the library's `complete_json_schema(prompt, PydanticModel)` — the CLI takes a raw
JSON file, not a Python class, so it uses the prompt-instruction path.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/subllm/cli.py tests/test_cli.py
git commit -m "feat: minimal CLI (complete / complete-json)"
```

---

## Phase 3 — Claude client (the heavy part)

> Adapt from `~/Projects/ralph-loop/ralph_lib/drivers.py`. Port the tmux spawn +
> JSONL completion-detection logic; DROP ralph's stream-json stdout emission and
> bash-watchdog coupling. The driver here is synchronous: prompt in → final text
> out.

### Task 8: claude_tmux driver

**Files:**
- Create: `src/subllm/drivers/claude_tmux.py`
- Test: `tests/test_claude_tmux.py`

The driver splits into two units: (a) `extract_final_text(jsonl_lines)` — the
correctness-critical completion/parse logic, fully unit-tested with fabricated
transcripts; (b) `run_claude_tmux(...)` — the tmux plumbing that produces those
lines. Test (a) exhaustively; smoke-test (b) with a fake `tmux`.

- [ ] **Step 1: Write the failing test for completion detection**

`tests/test_claude_tmux.py`:
```python
import json
import pytest
from subllm.drivers.claude_tmux import extract_final_text, _turn_complete
from subllm.errors import OutputError


def _assistant(text, stop_reason):
    return json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}],
                    "stop_reason": stop_reason},
    })


def test_ignores_partial_streaming_lines():
    lines = [
        _assistant("partial", None),       # streaming partial -> ignore
        _assistant("the answer", "end_turn"),
    ]
    assert extract_final_text(lines) == "the answer"


def test_turn_complete_only_on_end_turn():
    assert _turn_complete([_assistant("x", None)]) is False
    assert _turn_complete([_assistant("done", "end_turn")]) is True


def test_concatenates_multiple_text_blocks_in_final_turn():
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "part one. "},
            {"type": "text", "text": "part two."},
        ], "stop_reason": "end_turn"},
    })
    assert extract_final_text([line]) == "part one. part two."


def test_no_completed_turn_raises():
    with pytest.raises(OutputError):
        extract_final_text([_assistant("still thinking", None)])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_claude_tmux.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'subllm.drivers.claude_tmux'`.

- [ ] **Step 3: Implement completion detection + tmux plumbing**

`src/subllm/drivers/claude_tmux.py`:
```python
"""Drive interactive `claude` inside an ephemeral tmux pane and return the final
assistant text. Completion is detected by tailing the JSONL session transcript
for an assistant record with stop_reason == 'end_turn' (NOT by pane scraping).

Adapted from ralph-loop's ralph_lib/drivers.py, minus the bash-watchdog coupling.
Stdlib only.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from subllm.errors import ClientError, OutputError, QuotaError

_TERMINAL_STOP = {"end_turn"}


def _turn_complete(jsonl_lines: list[str]) -> bool:
    """True once any assistant record carries a terminal stop_reason."""
    for line in jsonl_lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "assistant":
            continue
        if rec.get("message", {}).get("stop_reason") in _TERMINAL_STOP:
            return True
    return False


def extract_final_text(jsonl_lines: list[str]) -> str:
    """Return the concatenated text of the last completed assistant turn.

    Only records with a terminal stop_reason count — partial streaming lines
    (stop_reason null) are ignored. Raises OutputError if no turn completed.
    """
    final_text: str | None = None
    for line in jsonl_lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message", {})
        if msg.get("stop_reason") not in _TERMINAL_STOP:
            continue
        parts = [
            b.get("text", "")
            for b in msg.get("content", [])
            if b.get("type") == "text"
        ]
        final_text = "".join(parts)
    if final_text is None:
        raise OutputError("no completed assistant turn found in transcript")
    return final_text


def _tmux(*args: str, timeout: float = 5.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["tmux", *args], capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as e:
        raise ClientError("tmux binary not found on PATH") from e


def _transcript_dir(config_dir: str, work_dir: str) -> Path:
    # Claude Code encodes the cwd into the project dir name (slashes -> dashes).
    encoded = work_dir.replace("/", "-")
    return Path(config_dir) / "projects" / encoded


def run_claude_tmux(
    prompt: str,
    *,
    model: str | None = None,
    permission_mode: str = "bypassPermissions",
    work_dir: str | None = None,
    config_dir: str | None = None,
    timeout_s: int = 300,
    poll_interval_s: float = 1.0,
) -> str:
    """Run one interactive claude turn via tmux; return final assistant text.

    Raises ClientError (tmux/spawn failure, timeout), QuotaError (limit banner),
    OutputError (no completed turn).
    """
    work_dir = work_dir or os.getcwd()
    config_dir = config_dir or os.environ.get(
        "CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")
    )
    session = f"subllm-{uuid.uuid4().hex[:8]}"
    tdir = _transcript_dir(config_dir, work_dir)
    before = set(tdir.glob("*.jsonl")) if tdir.exists() else set()

    argv = ["claude", "--permission-mode", permission_mode]
    if model:
        argv += ["--model", model]

    # ephemeral detached session running interactive claude
    res = _tmux(
        "new-session", "-d", "-s", session, "-x", "200", "-y", "50",
        "-c", work_dir, *argv,
    )
    if res.returncode != 0:
        raise ClientError(f"tmux new-session failed: {res.stderr.strip()[:200]}")

    try:
        _wait_for_ready(session, timeout_s=30)
        # send the prompt literally, then Enter as a separate key event
        _tmux("send-keys", "-t", session, "-l", prompt)
        _tmux("send-keys", "-t", session, "Enter")

        transcript = _await_completion(
            tdir, before, timeout_s=timeout_s, poll_interval_s=poll_interval_s,
            session=session,
        )
        return extract_final_text(transcript)
    finally:
        _tmux("kill-session", "-t", session)


def _wait_for_ready(session: str, timeout_s: float) -> None:
    """Wait until the claude TUI prompt is rendered before sending input."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pane = _tmux("capture-pane", "-p", "-t", session).stdout
        if _quota_banner(pane):
            raise QuotaError(f"claude limit banner: {pane.strip()[:200]}")
        if pane.strip():  # something rendered
            return
        time.sleep(0.5)
    raise ClientError("claude TUI did not become ready in time")


def _await_completion(
    tdir: Path, before: set, *, timeout_s: int, poll_interval_s: float, session: str
) -> list[str]:
    """Poll the (new) transcript file until a turn completes or we time out."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        current = set(tdir.glob("*.jsonl")) if tdir.exists() else set()
        new_files = current - before
        target = max(new_files, key=lambda p: p.stat().st_mtime) if new_files else None
        if target is not None:
            lines = target.read_text(errors="replace").splitlines()
            if _turn_complete(lines):
                return lines
        pane = _tmux("capture-pane", "-p", "-t", session).stdout
        if _quota_banner(pane):
            raise QuotaError(f"claude limit banner: {pane.strip()[:200]}")
        time.sleep(poll_interval_s)
    raise ClientError(f"claude turn did not complete within {timeout_s}s")


def _quota_banner(pane_text: str) -> bool:
    low = pane_text.lower()
    return any(s in low for s in ("usage limit", "rate limit", "out of credit"))
```

- [ ] **Step 4: Run completion-detection tests to verify they pass**

Run: `uv run pytest tests/test_claude_tmux.py -v`
Expected: PASS (4 passed). (Plumbing functions are covered by the smoke test in
Step 5.)

- [ ] **Step 5: Add a tmux-plumbing smoke test**

Append to `tests/test_claude_tmux.py`:
```python
def test_run_claude_tmux_happy_path(fake_bin, tmp_path, monkeypatch):
    # Fake claude: write a completed transcript into the encoded project dir.
    config_dir = tmp_path / "cfg"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    encoded = str(work_dir).replace("/", "-")
    proj = config_dir / "projects" / encoded
    proj.mkdir(parents=True)
    transcript = proj / "sess.jsonl"
    rec = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "tmux answer"}],
                    "stop_reason": "end_turn"},
    })
    # claude stub: render something to the pane, then drop a transcript line.
    fake_bin("claude", f'''#!/bin/bash
echo "ready prompt"
printf '%s\\n' '{rec}' > "{transcript}"
sleep 0.2
''')
    # fake tmux: run the claude argv in-process so the transcript appears.
    fake_bin("tmux", r'''#!/bin/bash
cmd="$1"; shift
case "$cmd" in
  new-session)
    # find the program after the last option; just exec claude
    claude >/dev/null 2>&1 &
    ;;
  capture-pane) echo "ready prompt" ;;
  send-keys|kill-session) : ;;
esac
exit 0
''')
    from subllm.drivers.claude_tmux import run_claude_tmux
    out = run_claude_tmux(
        "summarize", work_dir=str(work_dir), config_dir=str(config_dir),
        timeout_s=10, poll_interval_s=0.1,
    )
    assert out == "tmux answer"
```

Run: `uv run pytest tests/test_claude_tmux.py -v`
Expected: PASS (5 passed).

- [ ] **Step 6: Commit**

```bash
git add src/subllm/drivers/claude_tmux.py tests/test_claude_tmux.py
git commit -m "feat: claude tmux driver with JSONL completion detection"
```

---

### Task 9: ClaudeLLM

**Files:**
- Create: `src/subllm/claude.py`
- Modify: `src/subllm/__init__.py`
- Test: `tests/test_claude_llm.py`

- [ ] **Step 1: Write the failing test**

`tests/test_claude_llm.py`:
```python
import json
import pytest
from subllm.claude import ClaudeLLM
from subllm.errors import OutputError


def _patch_driver(monkeypatch, returns):
    import subllm.claude as mod
    monkeypatch.setattr(mod, "run_claude_tmux", lambda prompt, **kw: returns)


def test_complete_returns_text(monkeypatch):
    _patch_driver(monkeypatch, "claude says hi")
    assert ClaudeLLM().complete("x") == "claude says hi"


def test_complete_json_extracts_object_from_fenced_output(monkeypatch):
    _patch_driver(monkeypatch, 'Sure!\n```json\n{"k": 1}\n```\n')
    assert ClaudeLLM().complete_json("x") == {"k": 1}


def test_complete_json_raises_when_no_object(monkeypatch):
    _patch_driver(monkeypatch, "no json here")
    with pytest.raises(OutputError):
        ClaudeLLM(attempts=1).complete_json("x")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_claude_llm.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'subllm.claude'`.

- [ ] **Step 3: Write minimal implementation**

`src/subllm/claude.py`:
```python
"""ClaudeLLM — fallback client. Drives interactive claude over tmux.

JSON is prompt-engineered then validated (no native schema binding on this path).
"""
from __future__ import annotations

import json
import re
from typing import Any

from subllm.base import BaseLLM, _retry
from subllm.drivers.claude_tmux import run_claude_tmux
from subllm.errors import OutputError
from subllm.preflight import RegionGuard

_JSON_INSTRUCTION = "\n\nReturn ONLY a single JSON object — no prose, no code fence."
# Grab the first {...} block, tolerating ```json fences and surrounding prose.
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class ClaudeLLM(BaseLLM):
    def __init__(
        self,
        model: str | None = None,
        permission_mode: str = "bypassPermissions",
        region_guard: RegionGuard | None = None,
        attempts: int = 3,
        timeout_s: int = 300,
    ) -> None:
        self._model = model
        self._permission_mode = permission_mode
        self._guard = region_guard
        self._attempts = attempts
        self._timeout_s = timeout_s

    def _run(self, prompt: str) -> str:
        if self._guard is not None:
            self._guard.check()
        return run_claude_tmux(
            prompt,
            model=self._model,
            permission_mode=self._permission_mode,
            timeout_s=self._timeout_s,
        )

    def complete(self, prompt: str) -> str:
        return _retry(lambda: self._run(prompt), attempts=self._attempts)

    def complete_json(self, prompt: str) -> dict[str, Any]:
        def _call() -> dict[str, Any]:
            return _extract_json_object(self._run(prompt + _JSON_INSTRUCTION))
        return _retry(_call, attempts=self._attempts)


def _extract_json_object(raw: str) -> dict[str, Any]:
    match = _OBJECT_RE.search(raw or "")
    if not match:
        raise OutputError(f"no JSON object in claude output: {raw[:120]!r}")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise OutputError(f"claude returned invalid JSON: {raw[:120]!r}") from e
    if not isinstance(parsed, dict) or not parsed:
        raise OutputError("expected non-empty JSON object")
    return parsed
```

- [ ] **Step 4: Add ClaudeLLM to exports**

Edit `src/subllm/__init__.py`: add `from subllm.claude import ClaudeLLM` after the
`CodexLLM` import, and add `"ClaudeLLM"` to `__all__` after `"CodexLLM"`.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_claude_llm.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add src/subllm/claude.py src/subllm/__init__.py tests/test_claude_llm.py
git commit -m "feat: ClaudeLLM client"
```

---

## Phase 4 — Fallback + portability docs

### Task 10: FallbackLLM

**Files:**
- Create: `src/subllm/fallback.py`
- Modify: `src/subllm/__init__.py`
- Test: `tests/test_fallback.py`

- [ ] **Step 1: Write the failing test**

`tests/test_fallback.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_fallback.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'subllm.fallback'`.

- [ ] **Step 3: Write minimal implementation**

`src/subllm/fallback.py`:
```python
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
```

- [ ] **Step 4: Add FallbackLLM to exports**

Edit `src/subllm/__init__.py`: add `from subllm.fallback import FallbackLLM` after
the `ClaudeLLM` import, and add `"FallbackLLM"` to `__all__`.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_fallback.py -v`
Expected: PASS (4 passed).

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -q`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add src/subllm/fallback.py src/subllm/__init__.py tests/test_fallback.py
git commit -m "feat: explicit FallbackLLM composite"
```

---

### Task 11: drivers-contract docs (TS/Go portability boundary)

**Files:**
- Create: `docs/drivers-contract.md`

- [ ] **Step 1: Document the subprocess contract**

`docs/drivers-contract.md`:
```markdown
# Drivers contract

The `subllm/drivers/` package is the only language-specific boundary. A TS/Go
port reimplements just these two contracts; the rest of subllm (BaseLLM shape,
fallback, region guard) is trivial to re-express in any language.

## codex_exec

- Invoke: `codex exec -s read-only --skip-git-repo-check --ignore-user-config
  -o <tmpfile> [-m MODEL] [-c model_reasoning_effort="EFFORT"] [--search]
  [--output-schema FILE] -- PROMPT`
- Auth: ChatGPT subscription login (no API key). Set `CODEX_HOME` to an isolated
  dir containing only `auth.json` to avoid AGENTS.md context pollution.
- Output: the final message is written to `<tmpfile>` (the `-o` path); read it.
- Errors: non-zero exit with stderr matching /usage limit|rate.?limit|quota|
  too many requests|try again later/i  => QuotaError; missing binary / other
  non-zero exit / timeout => ClientError.

## claude_tmux

- Spawn interactive `claude --permission-mode bypassPermissions [--model M]`
  in an ephemeral tmux session (`tmux new-session -d -s <name> -c <workdir> ...`).
- Auth: regular Claude subscription (interactive path — NOT `claude -p`, which
  after 2026-06-15 bills a separate programmatic credit).
- Send prompt: `tmux send-keys -t <name> -l "<prompt>"` then a separate
  `tmux send-keys -t <name> Enter`.
- Detect completion: tail the newest JSONL under
  `${CLAUDE_CONFIG_DIR:-~/.claude}/projects/<cwd-with-slashes-as-dashes>/*.jsonl`.
  A turn is complete when an `{"type":"assistant", ...}` record has
  `message.stop_reason == "end_turn"`. IGNORE partial streaming lines
  (stop_reason null). Final text = concatenation of that record's text blocks.
- Always `tmux kill-session -t <name>` in a finally block.
- Errors: limit banner in pane => QuotaError; tmux/spawn failure/timeout =>
  ClientError; no completed turn => OutputError.
```

- [ ] **Step 2: Commit**

```bash
git add docs/drivers-contract.md
git commit -m "docs: drivers contract for TS/Go portability"
```

---

## Final verification

- [ ] **Run the whole suite**

Run: `uv run pytest -q`
Expected: all tests pass.

- [ ] **Smoke-test the CLI against the real codex subscription** (manual, optional)

Run:
```bash
mkdir -p /tmp/codex-clean   # copy your real ~/.codex/auth.json here once
CODEX_HOME=/tmp/codex-clean uv run subllm complete --client codex "Say hello in 5 words."
```
Expected: a short greeting on stdout. (This DOES hit your subscription — skip in CI.)

- [ ] **Update README status**

Edit `README.md`: change `**Status:** design complete, implementation not started.`
to `**Status:** v1 implemented (CodexLLM + ClaudeLLM + FallbackLLM + CLI).`
Commit:
```bash
git add README.md
git commit -m "docs: mark v1 implemented"
```
