"""Browser tests for drivers/chatgpt_web/page_core.js.

Loads DOM fixtures into a real ego lite page (about:blank + document.write) and
calls the pure page functions. Never navigates to chatgpt.com or any network
URL. Opt in: SUBLLM_BROWSER=1 (needs `ego-browser` on PATH).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        not os.environ.get("SUBLLM_BROWSER") or not shutil.which("ego-browser"),
        reason="needs SUBLLM_BROWSER=1 and ego-browser on PATH",
    ),
]

FIXTURES = Path(__file__).parent / "fixtures" / "chatgpt_web"
CORE = Path(__file__).parents[1] / "src" / "subllm" / "drivers" / "chatgpt_web" / "page_core.js"
PROMPT = "Write two short sentences about why the sky looks blue"
UUID = "11111111-2222-4333-8444-555555555555"
TAG = "SUBLLM_FIXTURE "


def call(name, *args):
    return "($CORE)." + name + "(" + ",".join(json.dumps(a) for a in args) + ")"


def turn_key(fixture):
    html = (FIXTURES / fixture).read_text()
    return re.search(r'data-turn-key="([^"]+)"', html).group(1)


COMPLETED = "redesign-completed-text.html"
KEY = turn_key(COMPLETED)

SET_EDITOR = (
    "(() => { const e = document.querySelector('div.ProseMirror[role=textbox]');"
    " e.innerHTML = '<p>line one</p><p>line two</p>'; return true; })()"
)
SETTLED = (
    "(() => { const c = ($CORE); const a = c.turnStatus(%s, null); return c.turnStatus(%s, a.text); })()"
)
CLIP_ROUNDTRIP = (
    "(async () => { const cb = navigator.clipboard; const w = cb && cb.writeText; const w2 = cb && cb.write;"
    " const r = await ($CORE).captureText(null);"
    " return { r, same: !cb || (navigator.clipboard.writeText === w && navigator.clipboard.write === w2) }; })()"
)

FAKE_CLIPBOARD = (
    "(() => { const proto = { writeText() { return 'orig'; }, write() { return 'origw'; } };"
    " Object.defineProperty(navigator, 'clipboard', { value: Object.create(proto), configurable: true });"
    " return true; })()"
)
COPY_VIA_WRITE_TEXT = (
    "(() => { document.querySelector('button[aria-label=\"Copy\"]').addEventListener('click',"
    " () => navigator.clipboard.writeText('**md** PINEAPPLE')); return true; })()"
)
COPY_VIA_WRITE = (
    "(() => { document.querySelector('button[aria-label=\"Copy\"]').addEventListener('click',"
    " () => navigator.clipboard.write([{ types: ['text/html', 'text/plain'],"
    " getType: async (t) => new Blob([t === 'text/plain' ? '**md2** PINEAPPLE' : '<b>x</b>']) }])); return true; })()"
)
SHOW_USER_COPY = (
    "(() => { const b = Array.from(document.querySelectorAll('button')).find("
    "x => x.getAttribute('aria-label') === 'Copy message');"
    " for (let n = b; n; n = n.parentElement) n.removeAttribute('hidden'); return true; })()"
)
SHOW_STOP = (
    "(() => { document.querySelector('button[aria-label=\"Stop\"]').removeAttribute('hidden'); return true; })()"
)

GROUPS = [
    ("redesign-idle-chat.html", [
        ("idle_state", call("state")),
        ("pre_editor", SET_EDITOR),
        ("cm_two_lines", call("composerMatches", "line one\nline two")),
        ("cm_blank_between", call("composerMatches", "line one\n\nline two")),
        ("cm_one_line", call("composerMatches", "line one")),
        ("cm_crlf_nbsp", call("composerMatches", "line one  \r\nline two\r\n")),
    ]),
    ("redesign-idle-work.html", [("work_state", call("state"))]),
    ("synthetic-logged-out.html", [("out_state", call("state"))]),
    ("synthetic-challenge.html", [("challenge_state", call("state"))]),
    ("synthetic-quota-dialog.html", [
        ("quota_state", call("state")),
        ("quota_turn", call("turnStatus", None, None)),
    ]),
    (COMPLETED, [
        ("done", SETTLED % (json.dumps(KEY), json.dumps(KEY))),
        ("first_read", call("turnStatus", KEY, None)),
        ("stable_probe", call("turnStatus", KEY, "x")),
        ("clip", CLIP_ROUNDTRIP),
        ("acc_placeholder", call("acceptance", f"https://chatgpt.com/c/local-chatgpt%3A{UUID}")),
        ("acc_server", call("acceptance", f"https://chatgpt.com/c/{UUID}")),
        ("acc_home", call("acceptance", "https://chatgpt.com/")),
        ("model", call("servedModel", KEY)),
        ("pre_fake_clip", FAKE_CLIPBOARD),
        ("clip_proto", CLIP_ROUNDTRIP),
        ("pre_copy_handler", COPY_VIA_WRITE_TEXT),
        ("clip_md", CLIP_ROUNDTRIP),
        ("unknown_key", call("turnStatus", "no-such-key", None)),
    ]),
    (COMPLETED, [
        ("pre_fake_clip2", FAKE_CLIPBOARD),
        ("pre_copy_handler2", COPY_VIA_WRITE),
        ("clip_md_write", CLIP_ROUNDTRIP),
    ]),
    ("synthetic-completed-hidden-stop.html", [
        ("hidden_stop_state", call("state")),
        ("hidden_stop_probe", SETTLED % (json.dumps(KEY), json.dumps(KEY))),
        ("pre_show_stop", SHOW_STOP),
        ("shown_stop_state", call("state")),
        ("shown_stop_probe", SETTLED % (json.dumps(KEY), json.dumps(KEY))),
    ]),
    ("synthetic-streaming.html", [
        ("streaming_state", call("state")),
        ("streaming_probe", SETTLED % (json.dumps(KEY), json.dumps(KEY))),
        ("pre_show_user_copy", SHOW_USER_COPY),
        ("user_copy_probe", SETTLED % (json.dumps(KEY), json.dumps(KEY))),
    ]),
    ("redesign-streaming-text.html", [
        ("live_streaming_state", call("state")),
        ("live_streaming_probe", SETTLED % (json.dumps(None), json.dumps(None))),
    ]),
    ("redesign-completed-markdown.html", [
        ("live_md_state", call("state")),
        ("live_md_probe", SETTLED % (json.dumps(None), json.dumps(None))),
        ("live_md_capture", call("captureText", None)),
    ]),
    ("synthetic-rejected-send.html", [
        ("acc_rejected", call("acceptance", f"https://chatgpt.com/c/local-chatgpt%3A{UUID}")),
        ("acc_other_path", call("acceptance", "https://chatgpt.com/c/not-a-uuid")),
        ("acc_rejected_server_url", call("acceptance", f"https://chatgpt.com/c/{UUID}")),
    ]),
]


def build_script(groups):
    payload = []
    for fixture, steps in groups:
        payload.append({"html": (FIXTURES / fixture).read_text(), "steps": steps})
    return (
        "const CORE = " + json.dumps(CORE.read_text()) + ";\n"
        "const GROUPS = " + json.dumps(payload) + ";\n"
        "const results = {};\n"
        'const task = await taskSpace("subllm-fixture-' + secrets.token_hex(4) + '");\n'
        "try {\n"
        '  const page = task.page("p1");\n'
        "  for (const g of GROUPS) {\n"
        '    await page.goto("about:blank");\n'
        "    await page.evaluate(h => { document.open(); document.write(h); document.close(); }, g.html);\n"
        "    for (const [id, expr] of g.steps) {\n"
        "      try { results[id] = await page.evaluate(expr.split('$CORE').join(CORE)); }\n"
        "      catch (e) { results[id] = { __error: String(e && e.message || e) }; }\n"
        "    }\n"
        "  }\n"
        "} finally {\n"
        "  await task.finish({ keep: [] });\n"
        "}\n"
        'console.log("' + TAG + '" + JSON.stringify(results));\n'
    )


@pytest.fixture(scope="module")
def r():
    proc = subprocess.run(
        ["ego-browser", "nodejs"],
        input=build_script(GROUPS),
        capture_output=True,
        text=True,
        timeout=180,
    )
    out = proc.stdout + "\n" + proc.stderr
    lines = [ln for ln in out.splitlines() if TAG in ln]
    assert lines, f"no result line (rc={proc.returncode}):\n{out[-2000:]}"
    results = json.loads(lines[-1].split(TAG, 1)[1])
    errors = {k: v["__error"] for k, v in results.items() if isinstance(v, dict) and "__error" in v}
    assert not errors, errors
    return results


def test_idle_chat_state(r):
    s = r["idle_state"]
    assert s["logged_in"] is True
    assert s["mode_surface"] == "chat"
    assert s["layout"] == "redesign"
    assert s["busy"] is False
    assert s["quota"] is False
    assert s["challenge"] is False


def test_idle_work_mode_surface(r):
    assert r["work_state"]["mode_surface"] == "work"


def test_logged_out(r):
    assert r["out_state"]["logged_in"] is False
    assert r["out_state"]["challenge"] is False


def test_challenge(r):
    assert r["challenge_state"]["challenge"] is True
    assert r["challenge_state"]["logged_in"] is False


def test_quota_dialog(r):
    assert r["quota_state"]["quota"] is True
    assert r["quota_state"]["logged_in"] is True
    assert r["quota_turn"]["error_dialog"]["kind"] == "quota"


def test_completed_turn_is_done_and_excludes_the_prompt(r):
    d = r["done"]
    assert d["copy_visible"] is True
    assert d["stop_visible"] is False
    assert "PINEAPPLE" in d["text"]
    assert PROMPT not in d["text"]
    assert "Write two short sentences" not in d["text"]
    assert d["images"] == []
    assert d["error_dialog"] is None
    assert d["text_stable"] is True
    assert d["done"] is True


def test_first_read_is_not_stable_or_done(r):
    f = r["first_read"]
    assert f["text_stable"] is False
    assert f["done"] is False


def test_unrelated_prev_text_is_not_stable(r):
    assert r["stable_probe"]["text_stable"] is False
    assert r["stable_probe"]["done"] is False


def test_unknown_key_falls_back_to_last_turn(r):
    assert "PINEAPPLE" in r["unknown_key"]["text"]


def test_capture_text_is_plain_without_a_copy_handler_and_restores_clipboard(r):
    c = r["clip"]
    assert c["same"] is True
    assert c["r"]["format"] == "plain"
    assert "PINEAPPLE" in c["r"]["text"]
    assert "Write two short sentences" not in c["r"]["text"]


def test_capture_text_uses_the_copy_buttons_write_text(r):
    assert r["clip_proto"]["same"] is True
    assert r["clip_proto"]["r"]["format"] == "plain"
    assert r["clip_md"]["same"] is True
    assert r["clip_md"]["r"] == {"text": "**md** PINEAPPLE", "format": "markdown"}


def test_capture_text_reads_text_plain_from_clipboard_items(r):
    assert r["clip_md_write"]["same"] is True
    assert r["clip_md_write"]["r"] == {"text": "**md2** PINEAPPLE", "format": "markdown"}


def test_a_rendered_stop_button_blocks_done_even_with_copy_and_stable_text(r):
    assert r["shown_stop_state"]["busy"] is True
    p = r["shown_stop_probe"]
    assert p["copy_visible"] is True and p["text_stable"] is True
    assert p["stop_visible"] is True
    assert p["done"] is False


def test_user_copy_message_button_is_not_the_assistant_copy(r):
    p = r["user_copy_probe"]
    assert p["copy_visible"] is False
    assert p["done"] is False


def test_unrecognised_conversation_path_stays_pending_even_with_an_error(r):
    assert r["acc_other_path"]["state"] == "pending"


def test_hidden_stop_does_not_block_done(r):
    assert r["hidden_stop_state"]["busy"] is False
    assert r["hidden_stop_probe"]["stop_visible"] is False
    assert r["hidden_stop_probe"]["done"] is True


def test_streaming_is_busy_and_not_done(r):
    assert r["streaming_state"]["busy"] is True
    p = r["streaming_probe"]
    assert p["stop_visible"] is True
    assert p["copy_visible"] is False
    assert p["done"] is False
    assert "PINEAPPLE" not in p["text"]
    assert p["text"]
    assert p["text_stable"] is True


def test_live_streaming_capture_is_busy_and_not_done(r):
    assert r["live_streaming_state"]["busy"] is True
    p = r["live_streaming_probe"]
    assert p["stop_visible"] is True
    assert p["copy_visible"] is False
    assert p["done"] is False
    assert p["text"] and "Answer in two parts" not in p["text"]


def test_live_completed_markdown_capture_after_copy(r):
    # Captured just after the Copy click, while its label read "Copied".
    assert r["live_md_state"]["busy"] is False
    p = r["live_md_probe"]
    assert p["stop_visible"] is False and p["text_stable"] is True
    assert p["text"].rstrip().endswith("PINEAPPLE")
    c = r["live_md_capture"]
    assert c["format"] == "plain"
    assert c["text"].rstrip().endswith("PINEAPPLE")
    assert "Red" in c["text"] and "Answer in two parts" not in c["text"]


def test_acceptance_placeholder_is_pending(r):
    a = r["acc_placeholder"]
    assert a == {"state": "pending", "turn_key": None, "conversation_url": None}


def test_acceptance_server_id_is_accepted(r):
    a = r["acc_server"]
    assert a["state"] == "accepted"
    assert a["conversation_url"] == f"https://chatgpt.com/c/{UUID}"
    assert a["turn_key"] == KEY


def test_acceptance_home_is_pending(r):
    assert r["acc_home"]["state"] == "pending"


def test_acceptance_rejected_send(r):
    assert r["acc_rejected"]["state"] == "rejected"
    assert r["acc_rejected_server_url"]["state"] == "accepted"


def test_composer_matches_normalises_both_sides(r):
    assert r["cm_two_lines"] is True
    assert r["cm_blank_between"] is True
    assert r["cm_crlf_nbsp"] is True
    assert r["cm_one_line"] is False


def test_served_model_is_null_on_redesign(r):
    assert r["model"] is None
