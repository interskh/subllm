"""Sanitize a live chatgpt.com DOM capture into a committable fixture.

    python sanitize.py IN.html OUT.html [--allow "synthetic text" ...]

Text nodes and aria-labels survive only if they are known UI chrome or a
string passed with --allow (the synthetic prompt and its answer). Everything
else becomes "Lorem ipsum". UUIDs map to fixed synthetic ids, links lose their
targets, non-asset image sources and inline styles are dropped, and <svg>,
<script> and <style> bodies are emptied. Review the printed residue before
committing.
"""
from __future__ import annotations

import argparse
import html
import re
import sys

UI_TEXT = {
    "Add files and more", "Ask ChatGPT", "Attach files", "Attach photos",
    "Attach photos or videos", "Chat", "ChatGPT", "ChatGPT can make mistakes. Check important info.",
    "ChatGPT said:", "Choose files to add", "Choose project", "Copied", "Composer mode",
    "Composer utility bar", "Conversation", "Copy", "Copy message", "Dictate",
    "Edit message", "Extra High", "Files", "Finding tasks for you…", "High",
    "How can I help,", "Latest response", "Light", "Log in", "Max", "Medium",
    "Minimal", "More actions", "None", "Open desktop app", "Persistent", "Plugins",
    "Rate response", "Read aloud", "Regenerate response", "Scroll to bottom",
    "See what Work can do", "Select ChatGPT model", "Select model", "Share",
    "Share prompt", "Sign up for free", "Start Voice", "Stop", "Stop streaming",
    "Take a photo", "Temporary chat", "Thinking", "Thinking effort", "Ultra",
    "What should we work on?", "Work", "Work with ChatGPT", "You", "You said:",
    "Send prompt", "Retry", "Unknown error", "GPT-6.1 Sol", "?",
}

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def sanitize(src: str, allow: set[str]) -> str:
    keep = UI_TEXT | allow
    for tag in ("svg", "script", "style"):
        src = re.sub(rf"<{tag}\b([^>]*)>.*?</{tag}>", rf"<{tag}\1></{tag}>", src, flags=re.S | re.I)
    src = re.sub(r'\sstyle="[^"]*"', "", src)
    src = re.sub(r'\shref="[^"]*"', ' href="#"', src)
    src = re.sub(r'\ssrc="(?!/cdn/assets/)[^"]*"', ' src=""', src)
    src = re.sub(r'\s(srcset|alt|title|data-placeholder)="[^"]*"', "", src)
    src = re.sub(r'\s(data-[a-z-]*(?:name|email)[a-z-]*)="[^"]*"', r' \1="User"', src)

    ids: dict[str, str] = {}

    def _uuid(m: re.Match) -> str:
        key = m.group(0).lower()
        if key not in ids:
            ids[key] = f"00000000-0000-4000-8000-{len(ids) + 1:012d}"
        return ids[key]

    src = UUID.sub(_uuid, src)

    def _aria(m: re.Match) -> str:
        val = html.unescape(m.group(1))
        return m.group(0) if val in keep else ' aria-label="Lorem ipsum"'

    src = re.sub(r'\saria-label="([^"]*)"', _aria, src)

    def _text(m: re.Match) -> str:
        raw = m.group(1)
        val = html.unescape(raw).strip()
        if not val or val in keep:
            return m.group(0)
        return ">Lorem ipsum<"

    return re.sub(r">([^<]+)<", _text, src)


def residue(doc: str) -> list[str]:
    text = re.sub(r"<[^>]+>", "\n", doc)
    return sorted({t.strip() for t in html.unescape(text).split("\n") if t.strip()})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--allow", action="append", default=[])
    ap.add_argument("--header", default="", help="HTML comment to prepend (capture date, state)")
    a = ap.parse_args()
    with open(a.src, encoding="utf-8") as fh:
        out = sanitize(fh.read(), set(a.allow))
    if a.header:
        out = f"<!-- {a.header} -->\n" + out
    with open(a.dst, "w", encoding="utf-8") as fh:
        fh.write(out)
    for line in residue(out):
        sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
