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


def _validate_with_model(parsed: dict[str, Any], schema_model: type) -> dict[str, Any]:
    """Validate a parsed dict against a Pydantic model; raise OutputError on
    mismatch. Shared by CodexLLM and ClaudeLLM so structured output is actually
    enforced, not merely requested."""
    from pydantic import ValidationError

    try:
        return schema_model.model_validate(parsed).model_dump()
    except ValidationError as e:
        raise OutputError(f"output failed schema validation: {e}") from e
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
            # NOTE: `--search` is NOT a valid `codex exec` flag in codex-cli
            # 0.136.0 (verified: `codex exec --search` -> "unexpected argument").
            # Web search for exec is enabled via config override instead.
            argv += ["-c", 'web_search="live"']
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
            # Inspect stderr AND stdout — codex prints limit notices to either.
            blob = f"{proc.stderr or ''}\n{proc.stdout or ''}".strip()
            if _QUOTA_PATTERNS.search(blob):
                raise QuotaError(f"codex subscription limit: {blob[:200]}")
            raise ClientError(
                f"codex exec failed (exit {proc.returncode}): {blob[:200]}"
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


def test_complete_json_schema_rejects_mismatch(fake_bin, tmp_path):
    # codex returns a JSON object that does NOT satisfy _Summary (missing 'topic')
    fake_bin("codex", r'''#!/bin/bash
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
printf '{"wrong": "field"}' > "$out"
''')
    llm = CodexLLM(model="gpt-5.4", codex_home=str(tmp_path), attempts=1)
    with pytest.raises(OutputError):
        llm.complete_json_schema("x", _Summary)


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

from subllm.base import BaseLLM, _retry, _validate_with_model
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
                parsed = _parse_json_object(self._run(prompt, schema_path=schema_path))
            finally:
                Path(schema_path).unlink(missing_ok=True)
            # Don't trust codex's binding blindly — validate against the model.
            return _validate_with_model(parsed, schema_model)
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
Expected: PASS (6 passed).

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

    try:
        args = parser.parse_args(argv)  # argparse exits(2) on bad args/choices
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
        # argparse (bad args/choices) or _build_client raised SystemExit.
        return e.code if isinstance(e.code, int) else 2


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

The driver splits into two units: (a) the pure completion/parse logic
(`extract_final_text` / `_turn_complete`) plus the deterministic-path and
command-builder helpers — fully unit-tested with fabricated transcripts; (b)
`run_claude_tmux(...)` — the tmux plumbing, smoke-tested with faithful fake
`tmux`/`claude` binaries. Test (a) exhaustively.

This is a faithful port of `ralph_lib/drivers.py`. Key mechanisms that MUST be
preserved (each was a codex-review finding): pass `--session-id` so the transcript
filename is deterministic; encode the *resolved* cwd with `re.sub(r"[^A-Za-z0-9-]","-")`;
pass `CLAUDE_CONFIG_DIR` into the tmux process via `-e`; build the claude command
as ONE shell string; handle the first-run trust dialog; use `load-buffer`/`paste-buffer`
for prompts with newlines or >4KB; complete only on an `end_turn` event that
contains text (not a thinking-only `end_turn`).

- [ ] **Step 1: Write the failing tests for the pure logic**

`tests/test_claude_tmux.py`:
```python
import json
from pathlib import Path
import pytest
from subllm.drivers.claude_tmux import (
    extract_final_text, _turn_complete, _project_jsonl_path, _build_claude_cmd,
)
from subllm.errors import OutputError


def _assistant(text=None, stop_reason=None, thinking=False):
    content = ([{"type": "thinking", "thinking": "hmm"}] if thinking
               else [{"type": "text", "text": text or ""}])
    return json.dumps({"type": "assistant",
                       "message": {"content": content, "stop_reason": stop_reason}})


def test_ignores_partial_streaming_lines():
    lines = [_assistant("partial", None), _assistant("the answer", "end_turn")]
    assert extract_final_text(lines) == "the answer"


def test_thinking_only_end_turn_is_not_complete():
    # Claude emits a thinking-only end_turn BEFORE the real text response.
    lines = [_assistant(stop_reason="end_turn", thinking=True)]
    assert _turn_complete(lines) is False
    with pytest.raises(OutputError):
        extract_final_text(lines)


def test_picks_text_end_turn_after_thinking_end_turn():
    lines = [
        _assistant(stop_reason="end_turn", thinking=True),  # thinking-only -> skip
        _assistant("real answer", "end_turn"),              # text response -> use
    ]
    assert _turn_complete(lines) is True
    assert extract_final_text(lines) == "real answer"


def test_concatenates_multiple_text_blocks():
    line = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "part one. "},
        {"type": "text", "text": "part two."},
    ], "stop_reason": "end_turn"}})
    assert extract_final_text([line]) == "part one. part two."


def test_no_completed_turn_raises():
    with pytest.raises(OutputError):
        extract_final_text([_assistant("still thinking", None)])


def test_project_jsonl_path_encoding(tmp_path):
    # non-alphanumerics (slashes, dots) -> '-'; filename is <session-id>.jsonl
    p = _project_jsonl_path(str(tmp_path / "cfg"), "/Users/example/git/x.y", "sess123")
    assert p.name == "sess123.jsonl"
    assert p.parent.name == "-Users-example-git-x-y"
    assert p.parent.parent.name == "projects"


def test_build_claude_cmd_is_single_string_with_session_and_disabled_tools():
    cmd = _build_claude_cmd("sess123", model="claude-x",
                            permission_mode="bypassPermissions", tools="")
    assert isinstance(cmd, str)
    assert "--session-id" in cmd and "sess123" in cmd
    assert "--permission-mode" in cmd and "bypassPermissions" in cmd
    assert "--model" in cmd and "claude-x" in cmd
    assert "--tools ''" in cmd  # tools disabled -> no approval deadlock
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_claude_tmux.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'subllm.drivers.claude_tmux'`.

- [ ] **Step 3: Implement completion detection + tmux plumbing**

`src/subllm/drivers/claude_tmux.py`:
```python
"""Drive interactive `claude` in an ephemeral tmux pane and return the final
assistant text. Completion = tailing the JSONL session transcript (deterministic
filename via --session-id) for an assistant `end_turn` event that actually
contains TEXT — not a thinking-only end_turn, not pane scraping.

Faithful port of ralph-loop's ralph_lib/drivers.py, minus the bash-watchdog
stream-json emission. Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid as uuid_mod
from pathlib import Path

from subllm.errors import ClientError, OutputError, QuotaError

_BANNER_MARKERS = ("❯", "│ >")            # claude TUI input-prompt indicators
_TRUST_DIALOG_MARKER = "Do you trust the files in this folder"
_QUOTA_MARKERS = ("usage limit", "rate limit", "out of credit")
_PASTE_PLACEHOLDER_RE = re.compile(r"\[Pasted text #\d+")
_ENV_PASSTHROUGH = ("CLAUDE_CONFIG_DIR", "PATH", "HOME", "USER", "LANG", "TERM")


# --- pure completion logic (unit-tested) ------------------------------------

def _completed_text(jsonl_lines: list[str]) -> str | None:
    """Text of the LAST assistant `end_turn` event that contains text blocks, or
    None. Thinking-only end_turn events and partial streaming lines (stop_reason
    null) do NOT count — otherwise the turn would 'complete' before the real
    answer is written."""
    result: str | None = None
    for line in jsonl_lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message", {})
        if msg.get("stop_reason") != "end_turn":
            continue
        text = "".join(
            b.get("text", "") for b in msg.get("content", []) if b.get("type") == "text"
        )
        if text:                       # require actual text, not thinking-only
            result = text
    return result


def _turn_complete(jsonl_lines: list[str]) -> bool:
    return _completed_text(jsonl_lines) is not None


def extract_final_text(jsonl_lines: list[str]) -> str:
    text = _completed_text(jsonl_lines)
    if text is None:
        raise OutputError("no completed text-bearing assistant turn in transcript")
    return text


# --- deterministic transcript path + command builder (unit-tested) ----------

def _project_jsonl_path(config_dir: str, work_dir: str, session_id: str) -> Path:
    """claude encodes the *resolved* cwd into the project dir name, replacing
    every non-alphanumeric/non-hyphen char with '-'. With --session-id the
    filename is exactly <session-id>.jsonl (no fragile newest-file globbing)."""
    encoded = re.sub(r"[^A-Za-z0-9-]", "-", str(Path(work_dir).resolve()))
    return Path(config_dir) / "projects" / encoded / f"{session_id}.jsonl"


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _build_claude_cmd(session_id: str, model: str | None, permission_mode: str,
                      tools: str | None) -> str:
    """ONE shell-command string for tmux's init command (tmux runs it via sh -c).
    Passing split argv after the window flags is mis-parsed by tmux."""
    parts = ["claude",
             f"--session-id {_q(session_id)}",
             f"--permission-mode {_q(permission_mode)}"]
    if model:
        parts.append(f"--model {_q(model)}")
    if tools is not None:
        parts.append(f"--tools {_q(tools)}")   # tools='' disables tools
    return " ".join(parts)


# --- tmux plumbing ----------------------------------------------------------

def _tmux(*args: str, timeout: float = 5.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["tmux", *args], capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError as e:
        raise ClientError("tmux binary not found on PATH") from e


def _capture(session: str) -> str:
    return _tmux("capture-pane", "-p", "-t", session).stdout


def _quota_banner(pane: str) -> bool:
    low = pane.lower()
    return any(s in low for s in _QUOTA_MARKERS)


def _env_args(config_dir: str) -> list[str]:
    """`-e KEY=VAL` pairs so the tmux/claude process writes its transcript where
    we read it. CLAUDE_CONFIG_DIR is the critical one."""
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = config_dir
    out: list[str] = []
    for k in _ENV_PASSTHROUGH:
        v = env.get(k)
        if v is not None:
            out += ["-e", f"{k}={v}"]
    return out


def run_claude_tmux(prompt: str, *, model: str | None = None,
                    permission_mode: str = "bypassPermissions",
                    tools: str | None = "", work_dir: str | None = None,
                    config_dir: str | None = None, timeout_s: int = 300,
                    poll_interval_s: float = 1.0) -> str:
    """Run one interactive claude turn via tmux; return final assistant text.
    tools='' (default) disables tools so an unattended turn never deadlocks on a
    tool-approval prompt. Raises QuotaError / ClientError / OutputError."""
    work_dir = work_dir or os.getcwd()
    config_dir = config_dir or os.environ.get(
        "CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")
    )
    session_id = str(uuid_mod.uuid4())
    session = f"subllm-{session_id[:8]}"
    jsonl_path = _project_jsonl_path(config_dir, work_dir, session_id)
    claude_cmd = _build_claude_cmd(session_id, model, permission_mode, tools)

    res = _tmux("new-session", "-d", "-s", session, "-x", "200", "-y", "50",
                "-c", work_dir, *_env_args(config_dir), claude_cmd)
    if res.returncode != 0:
        raise ClientError(f"tmux new-session failed: {res.stderr.strip()[:200]}")
    try:
        _wait_for_banner(session, timeout_s=30)
        _send_prompt(session, prompt)
        return extract_final_text(_await_completion(
            session, jsonl_path, timeout_s=timeout_s, poll_interval_s=poll_interval_s
        ))
    finally:
        _tmux("kill-session", "-t", session, timeout=3)


def _wait_for_banner(session: str, timeout_s: float) -> None:
    """Poll until the input prompt appears. On first run in a project the TUI
    shows a workspace-trust dialog first; accept it (send '1' + Enter) and keep
    polling — its own selection marker would otherwise be mistaken for the
    prompt."""
    deadline = time.monotonic() + timeout_s
    trust_handled = False
    while time.monotonic() < deadline:
        pane = _capture(session)
        if _quota_banner(pane):
            raise QuotaError(f"claude limit banner: {pane.strip()[:200]}")
        if not trust_handled and _TRUST_DIALOG_MARKER in pane:
            _tmux("send-keys", "-t", session, "1")
            time.sleep(0.2)
            _tmux("send-keys", "-t", session, "Enter")
            trust_handled = True
            time.sleep(1.0)
            continue
        if _TRUST_DIALOG_MARKER not in pane and any(m in pane for m in _BANNER_MARKERS):
            return
        time.sleep(0.5)
    raise ClientError("claude TUI prompt did not appear in time")


def _send_prompt(session: str, prompt: str) -> None:
    """send-keys -l for small single-line prompts; load-buffer + paste-buffer for
    multiline or >4KB (summaries routinely exceed 4KB). After a paste, wait for
    the bracketed-paste placeholder before Enter to avoid a dropped submit."""
    if "\n" in prompt or len(prompt.encode("utf-8")) > 4096:
        buf = f"subllm-{uuid_mod.uuid4().hex[:8]}"
        subprocess.run(["tmux", "load-buffer", "-b", buf, "-"], input=prompt,
                       text=True, check=True, timeout=5)
        _tmux("paste-buffer", "-p", "-d", "-b", buf, "-t", session)
        _wait_for_paste_placeholder(session)
    else:
        _tmux("send-keys", "-t", session, "-l", prompt)
        time.sleep(0.3)
    _tmux("send-keys", "-t", session, "Enter")


def _wait_for_paste_placeholder(session: str, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _PASTE_PLACEHOLDER_RE.search(_capture(session)):
            return
        time.sleep(0.1)
    # don't raise: pressing Enter anyway is cheaper than a full retry


def _await_completion(session: str, jsonl_path: Path, *, timeout_s: int,
                      poll_interval_s: float) -> list[str]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if jsonl_path.exists():
            lines = jsonl_path.read_text(errors="replace").splitlines()
            if _turn_complete(lines):
                return lines
        if _quota_banner(_capture(session)):
            raise QuotaError("claude limit banner during generation")
        time.sleep(poll_interval_s)
    raise ClientError(f"claude turn did not complete within {timeout_s}s")
```

- [ ] **Step 4: Run the pure-logic tests to verify they pass**

Run: `uv run pytest tests/test_claude_tmux.py -v`
Expected: PASS (7 passed). (Plumbing is covered by the smoke test in Step 5.)

- [ ] **Step 5: Add a faithful tmux-plumbing smoke test**

The fakes are Python (not bash) so they parse the real argv, honor `-e
CLAUDE_CONFIG_DIR`, assert the claude command is a SINGLE trailing string, and
write the transcript using the SAME path encoding + `--session-id` the driver
expects. A wrong command shape, missing env, or wrong path makes the test fail.

Append to `tests/test_claude_tmux.py`:
```python
_FAKE_CLAUDE = r'''#!/usr/bin/env python3
import os, re, sys, json
from pathlib import Path
args = sys.argv[1:]
sid = ""
for i, a in enumerate(args):
    if a == "--session-id" and i + 1 < len(args):
        sid = args[i + 1]
cfg = os.environ["CLAUDE_CONFIG_DIR"]            # KeyError if -e didn't pass it
encoded = re.sub(r"[^A-Za-z0-9-]", "-", str(Path.cwd().resolve()))
d = Path(cfg) / "projects" / encoded
d.mkdir(parents=True, exist_ok=True)
rec = {"type": "assistant",
       "message": {"content": [{"type": "text", "text": "tmux answer"}],
                   "stop_reason": "end_turn"}}
(d / (sid + ".jsonl")).write_text(json.dumps(rec) + "\n")
'''

_FAKE_TMUX = r'''#!/usr/bin/env python3
import os, sys, subprocess
a = sys.argv[1:]
sub = a[0] if a else ""
if sub == "new-session":
    rest, workdir, env = a[1:], None, dict(os.environ)
    i = 0
    while i < len(rest):
        if rest[i] == "-c" and i + 1 < len(rest):
            workdir = rest[i + 1]; i += 2; continue
        if rest[i] == "-e" and i + 1 < len(rest):
            k, _, v = rest[i + 1].partition("="); env[k] = v; i += 2; continue
        i += 1
    shell_cmd = rest[-1]
    assert "claude" in shell_cmd, "expected ONE claude command string, got: " + repr(shell_cmd)
    assert "CLAUDE_CONFIG_DIR" in env, "CLAUDE_CONFIG_DIR was not passed via -e"
    subprocess.Popen(["sh", "-c", shell_cmd], cwd=workdir, env=env)
    sys.exit(0)
if sub == "capture-pane":
    sys.stdout.write("❯ ")   # banner marker so _wait_for_banner proceeds
    sys.exit(0)
sys.exit(0)   # send-keys / load-buffer / paste-buffer / kill-session -> no-op
'''


def test_run_claude_tmux_happy_path(fake_bin, tmp_path):
    config_dir = tmp_path / "cfg"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    fake_bin("claude", _FAKE_CLAUDE)
    fake_bin("tmux", _FAKE_TMUX)
    from subllm.drivers.claude_tmux import run_claude_tmux
    out = run_claude_tmux("summarize", work_dir=str(work_dir),
                          config_dir=str(config_dir), timeout_s=10,
                          poll_interval_s=0.1)
    assert out == "tmux answer"
```

Run: `uv run pytest tests/test_claude_tmux.py -v`
Expected: PASS (8 passed).

- [ ] **Step 6: Commit**

```bash
git add src/subllm/drivers/claude_tmux.py tests/test_claude_tmux.py
git commit -m "feat: claude tmux driver (faithful ralph port, deterministic transcript)"
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
from pydantic import BaseModel
from subllm.claude import ClaudeLLM
from subllm.errors import OutputError


class _Summary(BaseModel):
    topic: str


def _patch_driver(monkeypatch, returns):
    import subllm.claude as mod
    monkeypatch.setattr(mod, "run_claude_tmux", lambda prompt, **kw: returns)


def test_complete_returns_text(monkeypatch):
    _patch_driver(monkeypatch, "claude says hi")
    assert ClaudeLLM().complete("x") == "claude says hi"


def test_complete_json_extracts_object_from_fenced_output(monkeypatch):
    _patch_driver(monkeypatch, 'Sure!\n```json\n{"k": 1}\n```\n')
    assert ClaudeLLM().complete_json("x") == {"k": 1}


def test_complete_json_handles_prose_braces_before_object(monkeypatch):
    # A greedy \{.*\} would mis-grab from the first brace in prose. raw_decode
    # scans candidate '{' positions and returns the first VALID object.
    _patch_driver(monkeypatch, 'note: use {curly} carefully. Here: {"k": 2} done')
    assert ClaudeLLM().complete_json("x") == {"k": 2}


def test_complete_json_raises_when_no_object(monkeypatch):
    _patch_driver(monkeypatch, "no json here")
    with pytest.raises(OutputError):
        ClaudeLLM(attempts=1).complete_json("x")


def test_complete_json_schema_validates(monkeypatch):
    _patch_driver(monkeypatch, '```json\n{"topic": "weekend"}\n```')
    assert ClaudeLLM().complete_json_schema("x", _Summary) == {"topic": "weekend"}


def test_complete_json_schema_rejects_mismatch(monkeypatch):
    _patch_driver(monkeypatch, '{"wrong": "field"}')
    with pytest.raises(OutputError):
        ClaudeLLM(attempts=1).complete_json_schema("x", _Summary)
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

from subllm.base import BaseLLM, _retry, _validate_with_model
from subllm.drivers.claude_tmux import run_claude_tmux
from subllm.errors import OutputError
from subllm.preflight import RegionGuard

_JSON_INSTRUCTION = "\n\nReturn ONLY a single JSON object — no prose, no code fence."


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

    def complete_json_schema(self, prompt: str, schema_model: type) -> dict[str, Any]:
        # Claude has no native schema binding — instruct + parse + validate.
        instruction = (
            _JSON_INSTRUCTION
            + " Match this JSON schema: "
            + json.dumps(schema_model.model_json_schema())
        )
        def _call() -> dict[str, Any]:
            parsed = _extract_json_object(self._run(prompt + instruction))
            return _validate_with_model(parsed, schema_model)
        return _retry(_call, attempts=self._attempts)


def _extract_json_object(raw: str) -> dict[str, Any]:
    """Extract the first VALID top-level JSON object. Strips ```json fences, then
    scans each '{' with json.JSONDecoder().raw_decode — robust against prose
    braces and trailing text (a greedy \\{.*\\} regex would mis-grab both)."""
    text = (raw or "").strip()
    if "```" in text:
        # keep the content of the first fenced block if present
        fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed:
            return parsed
    raise OutputError(f"no JSON object in claude output: {raw[:120]!r}")
```

- [ ] **Step 4: Add ClaudeLLM to exports**

Edit `src/subllm/__init__.py`: add `from subllm.claude import ClaudeLLM` after the
`CodexLLM` import, and add `"ClaudeLLM"` to `__all__` after `"CodexLLM"`.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_claude_llm.py -v`
Expected: PASS (6 passed).

- [ ] **Step 6: Commit**

```bash
git add src/subllm/claude.py src/subllm/__init__.py tests/test_claude_llm.py
git commit -m "feat: ClaudeLLM client (schema override + robust JSON extraction)"
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
  -o <tmpfile> [-m MODEL] [-c model_reasoning_effort="EFFORT"]
  [-c web_search="live"] [--output-schema FILE] -- PROMPT`
- NOTE: `--search` is NOT a valid `codex exec` flag (verified on codex-cli
  0.136.0: "unexpected argument '--search'"). Enable web search for exec via the
  config override `-c web_search="live"` instead.
- Auth: ChatGPT subscription login (no API key). Set `CODEX_HOME` to an isolated
  dir containing only `auth.json` to avoid AGENTS.md context pollution.
  `--ignore-user-config` still reads auth from `CODEX_HOME`.
- Output: the final message is written to `<tmpfile>` (the `-o` path); read it.
- Errors: non-zero exit with stderr+stdout matching /usage limit|rate.?limit|
  quota|too many requests|try again later/i => QuotaError; missing binary / other
  non-zero exit / timeout => ClientError.

## claude_tmux

- Build the claude command as ONE shell string (tmux runs it via `sh -c`; split
  trailing argv after the window flags is mis-parsed):
  `claude --session-id <uuid> --permission-mode bypassPermissions [--model M] --tools ''`.
  `--tools ''` disables tools so an unattended turn never deadlocks on approval.
- Spawn: `tmux new-session -d -s <name> -x 200 -y 50 -c <workdir>
  -e CLAUDE_CONFIG_DIR=<cfg> [-e PATH=… -e HOME=… …] '<claude-cmd>'`.
  Passing `CLAUDE_CONFIG_DIR` via `-e` is REQUIRED — otherwise claude writes its
  transcript somewhere the reader doesn't look and the turn times out.
- Auth: regular Claude subscription (interactive path — NOT `claude -p`, which
  after 2026-06-15 bills a separate programmatic credit).
- Readiness: poll `capture-pane` for the input-prompt markers (`❯`, `│ >`). On
  first run in a project a workspace-trust dialog ("Do you trust the files in
  this folder") appears first — accept it (send `1`, then `Enter`) and keep
  polling.
- Send prompt: for single-line ≤4KB, `send-keys -t <name> -l "<prompt>"` then a
  separate `send-keys -t <name> Enter`. For multiline or >4KB, `load-buffer` +
  `paste-buffer`, wait for the `[Pasted text #N]` placeholder, then `Enter`.
- Detect completion: read the DETERMINISTIC transcript
  `<cfg>/projects/<encoded>/<uuid>.jsonl`, where
  `encoded = re.sub(r"[^A-Za-z0-9-]", "-", str(Path(workdir).resolve()))` and
  `<uuid>` is the `--session-id`. A turn is complete when an
  `{"type":"assistant"}` record has `message.stop_reason == "end_turn"` AND
  contains a text content block. IGNORE partial streaming lines (stop_reason
  null) and thinking-only `end_turn` events. Final text = concatenation of that
  record's text blocks.
- Always `tmux kill-session -t <name>` in a finally block.
- Errors: limit banner in pane => QuotaError; tmux/spawn failure/timeout =>
  ClientError; no completed text turn => OutputError.
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
