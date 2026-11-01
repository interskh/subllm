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


@pytest.fixture(autouse=True)
def _no_real_chrome(request, monkeypatch):
    """Hermetic tests must never start Chrome (it would open chatgpt.com); a test that
    needs a fake replaces these seams itself."""
    if request.node.get_closest_marker("browser") or request.node.get_closest_marker("integration"):
        return
    from subllm.drivers.chatgpt_web import chrome

    def _launch(*args, **kwargs):
        pytest.fail("real Chrome launch in a hermetic test")

    def _spawn(*args, **kwargs):
        pytest.fail("real Chrome login spawn in a hermetic test")

    def _kill(*args, **kwargs):
        pytest.fail("real Chrome kill in a hermetic test")

    monkeypatch.setattr(chrome, "_kill_browser", _kill)
    monkeypatch.setattr(chrome, "_open_context", _launch)
    monkeypatch.setattr(chrome, "_spawn_login", _spawn)
