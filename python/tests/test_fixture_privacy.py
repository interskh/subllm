import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent / "fixtures"
FILES = sorted(p for p in ROOT.rglob("*") if p.is_file() and p.suffix != ".pyc" and "__pycache__" not in p.parts)

CHECKS = {
    "user token": re.compile(r"user-[A-Za-z0-9]{16,}"),
    "project token": re.compile(r"g-p-[0-9a-f]{32}"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@(?!example\.invalid)[A-Za-z0-9-]+\.[A-Za-z]{2,}"),  # .invalid is the reserved synthetic placeholder
    "home path": re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+/"),
}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
SYNTHETIC_UUID = re.compile(r"0{8}-0{4}-4000-8000-0{8}[0-9]{4}")


def test_fixture_corpus_is_found():
    assert any(p.suffix == ".html" for p in FILES) and any(p.suffix == ".jsonl" for p in FILES)


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_fixture_has_no_private_identifiers(path):
    text = path.read_text(errors="replace")
    for name, rx in CHECKS.items():
        assert not rx.search(text), f"{path.name}: {name}"
    # The recorded codex/claude CLI transcripts carry per-run session UUIDs (random, not account
    # data); only the browser captures, which carry account/conversation ids, must be synthetic.
    if "chatgpt_web" not in path.parts:
        return
    real = [u for u in UUID.findall(text) if not SYNTHETIC_UUID.fullmatch(u)]
    assert not real, f"{path.name}: {len(real)} non-synthetic UUID(s)"
