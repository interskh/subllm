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
