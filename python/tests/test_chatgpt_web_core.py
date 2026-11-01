"""Browser tests for drivers/chatgpt_web/page_core.js.

Loads DOM fixtures into a real browser page and calls the pure page functions, on two
substrates: ego lite (about:blank + document.write; needs `ego-browser` on PATH) and the
real ChromeDriver call path (patchright + Google Chrome, headless, throwaway profile,
every network request aborted). Never navigates to chatgpt.com or any network URL.
Opt in: SUBLLM_BROWSER=1.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import base64
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not os.environ.get("SUBLLM_BROWSER"), reason="needs SUBLLM_BROWSER=1"),
]

FIXTURES = Path(__file__).parent / "fixtures" / "chatgpt_web"
CORE = Path(__file__).parents[1] / "src" / "subllm" / "drivers" / "chatgpt_web" / "page_core.js"
PROMPT = "Write two short sentences about why the sky looks blue"
UUID = "11111111-2222-4333-8444-555555555555"
TAG = "SUBLLM_FIXTURE "


class Call(str):
    """A plain page_core call; the chrome substrate runs it through chrome.call_core."""


def call(name, *args):
    c = Call("($CORE)." + name + "(" + ",".join(json.dumps(a) for a in args) + ")")
    c.name, c.args = name, args
    return c


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

FOCUS_POWER = (
    "(() => { const r = document.querySelector('[role=menu] [data-reasoning-slider]'); r.focus();"
    " return document.activeElement === r; })()"
)
LOCK_SLIDER = (
    "(() => { document.querySelector('[data-locked]').setAttribute('data-locked', 'true'); return true; })()"
)
HIDE_MENU_SLIDER = (
    "(() => { document.querySelector('[data-reasoning-slider]').closest('[data-active]').setAttribute('aria-hidden', 'true');"
    " return true; })()"
)
INERT_SLIDER_ROW = (
    "(() => { const r = document.querySelector('[data-reasoning-slider]');"
    " r.parentElement.setAttribute('inert', ''); return true; })()"
)

MENU_FIXTURE = "redesign-completed-pro-menu.html"
MENU_KEY = turn_key(MENU_FIXTURE)
SEL_LABEL = (
    "(() => { const m = ($CORE).servedModelMenu(%s); const b = m && document.querySelector(m.selector);"
    " return b ? b.getAttribute('aria-label') : null; })()" % json.dumps(MENU_KEY)
)
NO_HASPOPUP = (
    "(() => { document.querySelector(\"button[aria-label='Regenerate response']\").removeAttribute('aria-haspopup');"
    " return true; })()"
)
MENU_THINKING = (
    "(() => { const s = Array.from(document.querySelectorAll('[role=menu] [role=menuitem] span span'))"
    ".find(x => x.textContent.includes('6 Pro')); s.textContent = ' \u2022 6 Thinking'; return true; })()"
)
MENU_OTHER_LABEL = (
    "(() => { document.querySelector('[role=menu]').setAttribute('aria-labelledby', 'radix-other'); return true; })()"
)
AUDIT_SERVED = "(() => { const c = ($CORE); return c.auditModel('pro', c.servedModel(%s)); })()"
REMOVE_MENU = "(() => { document.querySelector('[role=menu]').remove(); return true; })()"
CLOSE_MENU = (
    "(() => { document.querySelector('[role=menu]').setAttribute('data-state', 'closed'); return true; })()"
)

DECODE_IMAGES = "(async () => { await Promise.all(Array.from(document.images).map(i => i.decode())); return true; })()"

UPLOADING = "redesign-composer-attachment-uploading.html"
READY = "redesign-composer-attachment-ready.html"
MARKED_LABEL = (
    "(() => { const m = document.querySelectorAll('[data-subllm-ref-input]');"
    " return { n: m.length, label: m.length ? m[0].getAttribute('aria-label') : null }; })()"
)
SECOND_CHIP = (
    "(() => { const zone = document.querySelector('[data-composer-attachments] > div');"
    " const clone = zone.firstElementChild.cloneNode(true);"
    " clone.querySelectorAll('[aria-label]').forEach(e => e.setAttribute('aria-label',"
    " e.getAttribute('aria-label').replace('reference-1', 'reference-2')));"
    " zone.appendChild(clone); return true; })()"
)
SECOND_CHIP_UPLOADING = (
    "(() => { const zone = document.querySelector('[data-composer-attachments] > div');"
    " const bar = document.createElement('span'); bar.setAttribute('role', 'progressbar');"
    " bar.setAttribute('aria-label', 'Uploading reference-2.png');"
    " zone.lastElementChild.querySelector('img').after(bar); return true; })()"
)
CHIP_WITHOUT_DATA_PREVIEW = (
    "(() => { document.querySelectorAll('[data-composer-attachments] img')"
    ".forEach(i => i.setAttribute('src', 'https://chatgpt.com/synthetic.png')); return true; })()"
)
CHIP_WITHOUT_IMG = (
    "(() => { document.querySelectorAll('[data-composer-attachments] img').forEach(i => i.remove()); return true; })()"
)
TOOL_PILL_ONLY = (
    "(() => { document.querySelectorAll('[data-composer-attachments] button').forEach(b => b.remove());"
    " const f = document.querySelector('[data-composer-attachments]').closest('form');"
    " const b = document.createElement('button'); b.setAttribute('aria-label', 'Remove tool');"
    " f.insertBefore(b, f.firstChild); return true; })()"
)
EXTENSIONLESS_CHIP_OUTSIDE_ZONE = (
    "(() => { const f = document.querySelector('[data-composer-attachments]').closest('form');"
    " document.querySelectorAll('[data-composer-attachments] button').forEach(b => b.remove());"
    " const b = document.createElement('button'); b.setAttribute('aria-label', 'Remove notes.pdf');"
    " f.insertBefore(b, f.firstChild); return true; })()"
)
EXTENSIONLESS_CHIP_IN_ZONE = (
    "(() => { document.querySelectorAll(\"button[aria-label^='Remove ']\")"
    ".forEach(b => b.setAttribute('aria-label', 'Remove notes')); return true; })()"
)
ALERT_IN_ZONE = (
    "(() => { const a = document.createElement('div'); a.setAttribute('role', 'alert');"
    " document.querySelector('[data-composer-attachments]').appendChild(a); return true; })()"
)
ERROR_TEXT_IN_ZONE = (
    "(() => { const a = document.createElement('span'); a.textContent = 'Upload failed';"
    " document.querySelector('[data-composer-attachments]').appendChild(a); return true; })()"
)
SINGLE_INPUTS_ONLY = (
    "(() => { document.querySelectorAll('input[type=file]').forEach(i => {"
    " if (i.getAttribute('aria-label') !== 'Attach photos') i.remove(); else i.removeAttribute('multiple'); });"
    " return true; })()"
)
ONLY_CAMERA_INPUT = (
    "(() => { document.querySelectorAll('input[type=file]').forEach(i => {"
    " if (i.getAttribute('aria-label') !== 'Take a photo') i.remove(); }); return true; })()"
)
OUTSIDE_FORM = (
    "(() => { document.querySelectorAll('input[type=file]').forEach(i => document.body.appendChild(i));"
    " return true; })()"
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
    ("redesign-idle-chat.html", [
        ("pick_closed_medium_pro", call("selectMode", "pro")),
        ("pick_closed_medium_thinking", call("selectMode", "thinking")),
        ("pick_unknown", call("selectMode", "turbo")),
        ("pick_inherited_key", call("selectMode", "constructor")),
        ("pick_restore_level", call("selectMode", "Instant")),
    ]),
    ("redesign-picker-open-medium.html", [
        ("pick_open_medium_unfocused", call("selectMode", "pro")),
        ("open_menu_state", call("state")),
        ("open_menu_turn", call("turnStatus", None, None)),
        ("pre_focus_power", FOCUS_POWER),
        ("pick_open_medium_pro", call("selectMode", "pro")),
        ("pick_open_medium_instant", call("selectMode", "Instant")),
        ("pick_open_medium_thinking", call("selectMode", "thinking")),
        ("pre_lock_slider", LOCK_SLIDER),
        ("pick_locked", call("selectMode", "pro")),
        ("pick_locked_at_target", call("selectMode", "thinking")),
    ]),
    ("redesign-picker-open-medium.html", [
        ("pre_inert_row", INERT_SLIDER_ROW),
        ("pick_models_view", call("selectMode", "pro")),
    ]),
    ("redesign-picker-open-pro.html", [
        ("pick_open_pro_pro", call("selectMode", "pro")),
        ("pre_focus_power_pro", FOCUS_POWER),
        ("pick_open_pro_thinking", call("selectMode", "thinking")),
    ]),
    ("redesign-picker-closed-pro.html", [
        ("pick_closed_pro_pro", call("selectMode", "pro")),
        ("pick_closed_pro_thinking", call("selectMode", "thinking")),
        ("closed_pro_state", call("state")),
    ]),
    ("synthetic-logged-out.html", [("pick_no_trigger", call("selectMode", "pro"))]),
    (COMPLETED, [
        ("audit_null", call("auditModel", "pro", None)),
        ("audit_empty", call("auditModel", "pro", "")),
        ("audit_default", call("auditModel", "default", "GPT-6 Thinking")),
        ("audit_thinking", call("auditModel", "thinking", "gpt-5-6-thinking")),
        ("audit_pro_ok", call("auditModel", "pro", "GPT-6 Pro")),
        ("audit_pro_slug", call("auditModel", "pro", "gpt-6-pro")),
        ("audit_pro_down", call("auditModel", "pro", "GPT-6 Thinking")),
        ("audit_pro_down_slug", call("auditModel", "pro", "gpt-5-6-thinking")),
        ("audit_pro_word", call("auditModel", "pro", "gpt-6-professional")),
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
    ("synthetic-prompt-error-pending.html", [
        ("acc_prompt_error", call("acceptance", f"https://chatgpt.com/c/local-chatgpt%3A{UUID}")),
    ]),
    ("synthetic-rejected-send-error-prompt.html", [
        ("acc_rejected_error_prompt", call("acceptance", f"https://chatgpt.com/c/local-chatgpt%3A{UUID}")),
    ]),
    ("synthetic-completed-error-words.html", [
        ("error_words_probe", SETTLED % (json.dumps(KEY), json.dumps(KEY))),
    ]),
    ("synthetic-completed-image.html", [
        ("pre_decode", DECODE_IMAGES),
        ("image_probe", call("turnStatus", KEY, None)),
    ]),
    ("redesign-completed-image-reference.html", [
        ("pre_decode_ref", DECODE_IMAGES),
        ("ref_image_probe", call("turnStatus", None, None)),
    ]),
    (MENU_FIXTURE, [
        ("menu_model", call("servedModel", MENU_KEY)),
        ("menu_model_nokey", call("servedModel", None)),
        ("menu_btn", call("servedModelMenu", MENU_KEY)),
        ("menu_btn_label", SEL_LABEL),
        ("menu_state", call("state")),
        ("menu_audit_ok", call("auditModel", "pro", "6 Pro")),
        ("pre_other_labelledby", MENU_OTHER_LABEL),
        ("menu_wrong_owner", call("servedModel", MENU_KEY)),
    ]),
    (MENU_FIXTURE, [
        ("pre_close_menu", CLOSE_MENU),
        ("menu_closed_model", call("servedModel", MENU_KEY)),
        ("menu_closed_btn", call("servedModelMenu", MENU_KEY)),
    ]),
    (MENU_FIXTURE, [
        ("pre_no_haspopup", NO_HASPOPUP),
        ("menu_no_popup", call("servedModelMenu", MENU_KEY)),
    ]),
    (MENU_FIXTURE, [
        ("pre_thinking", MENU_THINKING),
        ("menu_thinking", call("servedModel", MENU_KEY)),
        ("menu_thinking_audit", AUDIT_SERVED % json.dumps(MENU_KEY)),
    ]),
    (MENU_FIXTURE, [
        ("pre_remove_menu", REMOVE_MENU),
        ("menu_removed", call("servedModel", MENU_KEY)),
    ]),
    (COMPLETED, [("completed_menu_btn", call("servedModelMenu", KEY))]),
    ("synthetic-legacy-error-alert.html", [
        ("legacy_alert_probe", SETTLED % (json.dumps(None), json.dumps(None))),
    ]),
    ("redesign-idle-chat.html", [
        ("att_idle", call("attachments")),
        ("ref_idle_1", call("referenceInput", 1)),
        ("ref_idle_1_marked", MARKED_LABEL),
        ("ref_idle_3", call("referenceInput", 3)),
        ("ref_idle_3_marked", MARKED_LABEL),
    ]),
    (UPLOADING, [("att_uploading", call("attachments"))]),
    (READY, [
        ("att_ready", call("attachments")),
        ("pre_editor_att", SET_EDITOR),
        ("cm_with_attachment", call("composerMatches", "line one\nline two")),
        ("cm_with_attachment_mismatch", call("composerMatches", "line one")),
        ("pre_second_chip", SECOND_CHIP),
        ("att_two_ready", call("attachments")),
        ("pre_second_uploading", SECOND_CHIP_UPLOADING),
        ("att_two_one_pending", call("attachments")),
    ]),
    (READY, [
        ("pre_other_src", CHIP_WITHOUT_DATA_PREVIEW),
        ("att_other_src", call("attachments")),
    ]),
    (READY, [
        ("pre_no_img", CHIP_WITHOUT_IMG),
        ("att_no_img", call("attachments")),
    ]),
    (READY, [
        ("pre_tool_pill", TOOL_PILL_ONLY),
        ("att_tool_pill", call("attachments")),
    ]),
    (READY, [
        ("pre_zone_chip", EXTENSIONLESS_CHIP_IN_ZONE),
        ("att_zone_chip", call("attachments")),
    ]),
    (READY, [
        ("pre_file_label", EXTENSIONLESS_CHIP_OUTSIDE_ZONE),
        ("att_file_label", call("attachments")),
    ]),
    (READY, [
        ("pre_alert", ALERT_IN_ZONE),
        ("att_alert", call("attachments")),
    ]),
    (READY, [
        ("pre_error_text", ERROR_TEXT_IN_ZONE),
        ("att_error_text", call("attachments")),
    ]),
    ("redesign-idle-chat.html", [
        ("pre_single_inputs", SINGLE_INPUTS_ONLY),
        ("ref_single_1", call("referenceInput", 1)),
        ("ref_single_1_marked", MARKED_LABEL),
        ("ref_single_2", call("referenceInput", 2)),
    ]),
    ("redesign-idle-chat.html", [
        ("pre_camera_only", ONLY_CAMERA_INPUT),
        ("ref_camera_only", call("referenceInput", 1)),
    ]),
    ("redesign-idle-chat.html", [
        ("pre_outside_form", OUTSIDE_FORM),
        ("ref_outside_form", call("referenceInput", 1)),
    ]),
    ("synthetic-logged-out.html", [
        ("att_no_composer", call("attachments")),
        ("ref_no_composer", call("referenceInput", 1)),
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


def _run_ego():
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
    return json.loads(lines[-1].split(TAG, 1)[1])


# Worlds mirror ChromeDriver: pre_* steps stand in for the page's own scripts and the
# clipboard round trips need the page's world (captureText runs main-world in the driver);
# every other core call is isolated, as call_core does by default.
def _chrome_world_main(step_id, expr):
    return step_id.startswith("pre_") or expr is CLIP_ROUNDTRIP or (
        isinstance(expr, Call) and expr.name == "captureText"
    )


def _run_chrome(profile_dir):
    from patchright.sync_api import sync_playwright

    from subllm.drivers.chatgpt_web import chrome

    results = {}
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir), channel="chrome", headless=True
        )
        try:
            ctx.route("**/*", lambda route: route.abort())
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            for fixture, steps in GROUPS:
                page.goto("about:blank")
                page.set_content((FIXTURES / fixture).read_text())
                for step_id, expr in steps:
                    main = _chrome_world_main(step_id, expr)
                    try:
                        if isinstance(expr, Call):
                            results[step_id] = chrome.call_core(page, expr.name, *expr.args, main_world=main)
                        else:
                            results[step_id] = page.evaluate(
                                expr.replace("$CORE", CORE.read_text()), isolated_context=not main
                            ) if "$CORE" in expr else page.evaluate(expr, isolated_context=not main)
                    except Exception as e:
                        results[step_id] = {"__error": str(e)}
        finally:
            ctx.close()
    return results


def _chrome_skip_reason():
    try:
        import patchright.sync_api  # noqa: F401
    except ImportError:
        return "patchright is not installed"
    from subllm.drivers.chatgpt_web import chrome

    if chrome._chrome_executable() is None:
        return "Google Chrome is not installed"
    return None


@pytest.fixture(scope="module", params=["ego", "chrome"])
def r(request, tmp_path_factory):
    if request.param == "ego":
        if not shutil.which("ego-browser"):
            pytest.skip("ego-browser is not on PATH")
        results = _run_ego()
    else:
        reason = _chrome_skip_reason()
        if reason:
            pytest.skip(reason)
        results = _run_chrome(tmp_path_factory.mktemp("chrome-profile"))
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


def test_served_model_comes_from_the_open_try_again_menu(r):
    assert r["menu_model"] == "6 Pro"
    assert r["menu_model_nokey"] == "6 Pro"
    assert r["menu_audit_ok"] == "ok"


def test_served_model_menu_selects_this_turns_regenerate_button(r):
    assert r["menu_btn"]["selector"].startswith("#")
    assert r["menu_btn_label"] == "Regenerate response"


def test_served_model_menu_needs_the_popup_declaration(r):
    assert r["menu_no_popup"] is None


def test_served_model_ignores_a_menu_owned_by_another_button(r):
    assert r["menu_wrong_owner"] is None


def test_served_model_ignores_a_closed_menu(r):
    assert r["menu_closed_model"] is None
    assert r["menu_closed_btn"]["selector"].startswith("#")


def test_served_model_menu_label_is_what_the_audit_judges(r):
    assert r["menu_thinking"] == "6 Thinking"
    assert r["menu_thinking_audit"] == "downgrade"


def test_served_model_is_null_when_the_menu_is_gone(r):
    assert r["menu_removed"] is None


def test_menu_fixture_is_not_a_quota_state(r):
    assert r["menu_state"]["quota"] is False


def test_closed_menu_completed_fixture_offers_the_button_but_no_label(r):
    assert r["completed_menu_btn"]["selector"].startswith("#")
    assert r["model"] is None


def test_login_prompt_flag(r):
    assert r["out_state"]["login_prompt"] is True
    assert r["idle_state"]["login_prompt"] is False
    assert r["challenge_state"]["login_prompt"] is False


def test_prompt_starting_with_an_error_phrase_is_not_a_rejection(r):
    assert r["acc_prompt_error"]["state"] == "pending"


def test_real_error_element_is_still_a_rejection_with_an_error_phrase_prompt(r):
    assert r["acc_rejected_error_prompt"]["state"] == "rejected"


def test_prompt_and_answer_starting_with_unknown_error_are_not_an_error_dialog(r):
    p = r["error_words_probe"]
    assert p["error_dialog"] is None
    assert p["done"] is True


def test_legacy_error_alert_beside_the_answer_body_is_an_error_dialog(r):
    p = r["legacy_alert_probe"]
    assert p["error_dialog"] and p["error_dialog"]["text"].startswith("Something went wrong while")


def test_image_probe_excludes_icon_and_user_image(r):
    html = (FIXTURES / "synthetic-completed-image.html").read_text()
    user_img, icon, big = re.findall(r'<img[^>]*? src="([^"]+)"', html)
    assert len({user_img, icon, big}) == 3
    assert r["image_probe"]["images"] == [big]
    assert "PINEAPPLE" in r["image_probe"]["text"]


def test_image_probe_excludes_the_reference_attachment_in_the_user_turn(r):
    html = (FIXTURES / "redesign-completed-image-reference.html").read_text()
    thumb, generated = [s for s in re.findall(r'<img[^>]*? src="([^"]+)"', html) if len(s) > 500]
    assert thumb != generated
    assert r["ref_image_probe"]["images"] == [generated]


def test_chrome_image_fetch_writes_the_fixture_bytes(tmp_path_factory):
    """Chrome-only: the driver's real _FETCH_JS (main world) returns the image bytes."""
    reason = _chrome_skip_reason()
    if reason:
        pytest.skip(reason)
    from patchright.sync_api import sync_playwright

    from subllm.drivers.chatgpt_web import chrome

    html = (FIXTURES / "synthetic-completed-image.html").read_text()
    big = re.findall(r'<img[^>]*? src="([^"]+)"', html)[2]
    m = re.match(r"data:image/png;base64,(.+)$", big)
    assert m, "the fixture's big image must be a PNG data: URL"
    expected = base64.b64decode(m.group(1), validate=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(tmp_path_factory.mktemp("chrome-profile")), channel="chrome", headless=True
        )
        try:
            ctx.route("**/*", lambda route: route.abort())
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("about:blank")
            page.set_content(html)
            page.evaluate(DECODE_IMAGES, isolated_context=False)
            src = chrome.call_core(page, "turnStatus", KEY, None)["images"][0]
            assert src == big
            b64 = page.evaluate(chrome._FETCH_JS % json.dumps(src), isolated_context=False)
        finally:
            ctx.close()
    got = base64.b64decode(b64, validate=True)
    assert got == expected
    assert got.startswith(b"\x89PNG\r\n\x1a\n")


def test_chrome_capture_text_needs_the_main_world_through_call_core(tmp_path_factory):
    """Chrome-only: the page's Copy handler lives in the main world, so call_core's
    main_world flag decides whether captureText sees it."""
    reason = _chrome_skip_reason()
    if reason:
        pytest.skip(reason)
    from patchright.sync_api import sync_playwright

    from subllm.drivers.chatgpt_web import chrome

    html = (FIXTURES / COMPLETED).read_text()
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(tmp_path_factory.mktemp("chrome-profile")), channel="chrome", headless=True
        )
        try:
            ctx.route("**/*", lambda route: route.abort())
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            out = {}
            for main in (True, False):
                page.goto("about:blank")
                page.set_content(html)
                page.evaluate(FAKE_CLIPBOARD, isolated_context=False)
                page.evaluate(COPY_VIA_WRITE_TEXT, isolated_context=False)
                out[main] = chrome.call_core(page, "captureText", None, main_world=main)
        finally:
            ctx.close()
    assert out[True] == {"text": "**md** PINEAPPLE", "format": "markdown"}
    assert out[False]["format"] == "plain"


def test_planner_closed_at_medium(r):
    p = r["pick_closed_medium_pro"]
    assert p == {"action": "click", "selector": "button[aria-label='Select ChatGPT model']", "current": "Medium"}
    assert r["pick_closed_medium_thinking"] == {"action": "done", "selected_label": "Medium", "current": "Medium"}
    assert r["pick_restore_level"]["action"] == "click"


def test_planner_rejects_unknown_modes(r):
    assert r["pick_unknown"]["action"] == "fail" and "unknown mode" in r["pick_unknown"]["detail"]
    assert r["pick_inherited_key"]["action"] == "fail"


def test_planner_open_medium_focuses_then_steps_by_direction(r):
    f = r["pick_open_medium_unfocused"]
    assert f == {"action": "focus", "selector": "[role=menu] [data-reasoning-slider]", "current": "Medium"}
    assert r["pick_open_medium_pro"] == {"action": "key", "key": "ArrowRight", "current": "Medium"}
    assert r["pick_open_medium_instant"] == {"action": "key", "key": "ArrowLeft", "current": "Medium"}


def test_planner_closes_the_menu_once_at_the_target(r):
    assert r["pick_open_medium_thinking"] == {"action": "key", "key": "Escape", "current": "Medium"}
    assert r["pick_open_pro_pro"] == {"action": "key", "key": "Escape", "current": "Pro"}
    assert r["pick_locked_at_target"]["action"] == "key" and r["pick_locked_at_target"]["key"] == "Escape"


def test_planner_open_pro_steps_left_for_a_lower_level(r):
    assert r["pick_open_pro_thinking"] == {"action": "key", "key": "ArrowLeft", "current": "Pro"}


def test_planner_closed_at_pro_reads_the_trigger_label(r):
    assert r["pick_closed_pro_pro"] == {"action": "done", "selected_label": "Pro", "current": "Pro"}
    assert r["pick_closed_pro_thinking"]["action"] == "click" and r["pick_closed_pro_thinking"]["current"] == "Pro"


def test_planner_never_trusts_the_selected_effort_attribute(r):
    html = (FIXTURES / "redesign-picker-closed-pro.html").read_text()
    # the live trigger attribute disagreed with its label; the label wins
    assert 'data-selected-reasoning-effort="medium"' in html
    assert r["pick_closed_pro_pro"]["current"] == "Pro"


def test_planner_locked_slider_fails(r):
    p = r["pick_locked"]
    assert p["action"] == "fail" and "locked" in p["detail"] and p["current"] == "Medium"


def test_planner_ignores_the_hidden_models_view(r):
    assert r["pick_models_view"] == {"action": "key", "key": "Escape", "current": None}


def test_planner_without_a_trigger_fails_closed(r):
    p = r["pick_no_trigger"]
    assert p["action"] == "fail" and "picker not found" in p["detail"]


def test_open_picker_menu_is_not_a_quota_signal(r):
    assert r["open_menu_state"]["quota"] is False
    assert r["open_menu_state"]["logged_in"] is True
    assert r["open_menu_turn"]["error_dialog"] is None
    html = (FIXTURES / "redesign-picker-open-medium.html").read_text()
    assert "Usage limit reached" in html


def test_audit_model(r):
    assert r["audit_null"] == "unverified" and r["audit_empty"] == "unverified"
    assert r["audit_default"] == "ok" and r["audit_thinking"] == "ok"
    assert r["audit_pro_ok"] == "ok" and r["audit_pro_slug"] == "ok"
    assert r["audit_pro_down"] == "downgrade" and r["audit_pro_down_slug"] == "downgrade"
    assert r["audit_pro_word"] == "downgrade"


def test_idle_composer_has_no_attachments(r):
    assert r["att_idle"] == {"count": 0, "pending": 0, "ready": 0, "failed": 0}


def test_reference_input_is_the_multiple_image_input_not_the_camera_one(r):
    assert r["ref_idle_1"] == {"selector": "input[data-subllm-ref-input]"}
    assert r["ref_idle_1_marked"] == {"n": 1, "label": "Attach photos"}
    assert r["ref_idle_3_marked"] == {"n": 1, "label": "Attach photos"}


def test_reference_input_never_selects_by_generated_id(r):
    assert "id" not in r["ref_idle_1"]["selector"] and "#" not in r["ref_idle_1"]["selector"]


def test_reference_input_single_file_may_use_a_single_input_but_many_need_multiple(r):
    assert r["ref_single_1_marked"] == {"n": 1, "label": "Attach photos"}
    assert r["ref_single_2"] is None


def test_reference_input_refuses_the_camera_input_and_inputs_outside_the_form(r):
    assert r["ref_camera_only"] is None
    assert r["ref_outside_form"] is None


def test_attachments_and_reference_input_without_a_composer(r):
    assert r["att_no_composer"] == {"count": 0, "pending": 0, "ready": 0, "failed": 0}
    assert r["ref_no_composer"] is None


def test_uploading_chip_is_counted_but_pending(r):
    assert r["att_uploading"] == {"count": 1, "pending": 1, "ready": 0, "failed": 0}


def test_finished_chip_is_ready(r):
    assert r["att_ready"] == {"count": 1, "pending": 0, "ready": 1, "failed": 0}


def test_a_remove_tool_control_is_not_an_attachment_chip(r):
    assert r["att_tool_pill"]["count"] == 0
    assert r["att_file_label"]["count"] == 1
    assert r["att_zone_chip"]["count"] == 1


def test_a_chip_is_ready_only_with_a_data_image_preview(r):
    assert r["att_other_src"] == {"count": 1, "pending": 0, "ready": 0, "failed": 0}
    assert r["att_no_img"] == {"count": 1, "pending": 0, "ready": 0, "failed": 0}


def test_two_chips_one_still_uploading(r):
    assert r["att_two_ready"] == {"count": 2, "pending": 0, "ready": 2, "failed": 0}
    assert r["att_two_one_pending"] == {"count": 2, "pending": 1, "ready": 1, "failed": 0}


def test_composer_matches_with_an_attachment_present(r):
    assert r["cm_with_attachment"] is True
    assert r["cm_with_attachment_mismatch"] is False


def test_an_alert_or_error_text_in_the_attachment_area_is_a_failure(r):
    assert r["att_alert"]["failed"] >= 1
    assert r["att_error_text"]["failed"] >= 1
