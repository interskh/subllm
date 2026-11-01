"""Drive chatgpt.com in Google Chrome with patchright (optional extra `subllm[chrome]`).

A Python port of ego_run.js: same page_core.js, same constants, same outcome mapping
(ego.classify). The send key is the point of no return: `rec.sent` is set before the
first key event, and any failure after it is never retried.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from subllm.base import ImageResult, WebTextResult
from subllm.drivers.chatgpt_web.ego import _collect_images, chatgpt_lock, classify, normalize_prompt
from subllm.errors import ClientError

_HERE = Path(__file__).parent
# patchright's channel="chrome" lookup (coreBundle.js _createChromiumChannel), per platform
_CHROME_PATHS = {
    "darwin": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "linux": "/opt/google/chrome/chrome",
    "win32": "\\Google\\Chrome\\Application\\chrome.exe",
}
_WIN_PREFIX_ENV = ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")
_LOGIN_ARGV = [sys.executable, "-m", "subllm.drivers.chatgpt_web.chrome", "login"]
_HOME_URL = "https://chatgpt.com/"

_FETCH_JS = """(async (src) => {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), 30000);
  try {
    const r = await fetch(src, { signal: ac.signal });
    if (!r.ok) throw new Error("image fetch failed: " + r.status);
    const blob = await r.blob();
    const url = await new Promise((ok, bad) => {
      const fr = new FileReader();
      fr.onload = () => ok(fr.result);
      fr.onerror = () => bad(fr.error);
      fr.readAsDataURL(blob);
    });
    return url.slice(url.indexOf(",") + 1);
  } finally {
    clearTimeout(timer);
  }
})(%s)"""

# patched by tests: a clock that advances on sleep keeps the loops instant
_clock = time.monotonic
_sleep = time.sleep


def profile_dir() -> Path:
    return Path.home() / ".local/share/subllm/chrome-profile"


def _chrome_candidates() -> list[str]:
    suffix = _CHROME_PATHS.get(sys.platform)
    if suffix is None:
        return []
    if sys.platform != "win32":
        return [suffix]
    drive = os.environ.get("HOMEDRIVE", "")
    prefixes = [os.environ.get(k) for k in _WIN_PREFIX_ENV] + [drive + "\\Program Files", drive + "\\Program Files (x86)"]
    return [p + suffix for p in prefixes if p]


def _chrome_executable() -> str | None:
    for p in _chrome_candidates():
        if Path(p).is_file() and os.access(p, os.R_OK):
            return p
    return None


def _browser_pid(profile: Path) -> int | None:
    # Chrome's SingletonLock is a symlink "<host>-<pid>" inside the profile dir
    try:
        pid = int(os.readlink(profile / "SingletonLock").rsplit("-", 1)[1])
    except (OSError, ValueError, IndexError):
        return None
    return pid if pid > 1 else None


def _kill_browser(profile: Path) -> None:
    """SIGKILL the Chrome that owns this profile (never by name)."""
    pid = _browser_pid(profile)
    if pid is not None:
        os.kill(pid, signal.SIGKILL)


def _budget(timeout_s: float, prompt: str, kind: str) -> float:
    # same formula as the ego subprocess budget
    return timeout_s + 120 + int(len(prompt) * 0.06) + (60 if kind == "image" else 0)


@contextlib.contextmanager
def _open_context(profile: Path):
    """The one place a real browser starts (the login spawn is the other seam)."""
    from patchright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile), channel="chrome", headless=False, no_viewport=True
        )
        try:
            yield ctx
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def _spawn_login() -> None:
    subprocess.Popen(
        _LOGIN_ARGV,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


_CORE: str | None = None


def call_core(page, name: str, *args, main_world: bool = False):
    """Call a page_core.js function. patchright evaluates in an isolated world by default;
    captureText and the image fetch need the page's own world."""
    global _CORE
    if _CORE is None:
        _CORE = (_HERE / "page_core.js").read_text()
    expr = "(" + _CORE + ")." + name + "(" + ",".join(json.dumps(a) for a in args) + ")"
    return page.evaluate(expr, isolated_context=not main_world)


class _Stop(Exception):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


class _Rec:
    def __init__(self) -> None:
        self.sent = False
        self.dead = False
        self.url: str | None = None
        self.events: list[dict] = []


class ChromeDriver:
    def available(self) -> tuple[bool, str]:
        try:
            import patchright.sync_api  # noqa: F401
        except ImportError:
            return False, "patchright is not installed (pip install 'subllm[chrome]')"
        if _chrome_executable() is None:
            where = (_chrome_candidates() or ["a supported location"])[0]
            return False, f"Google Chrome is not installed at {where}; the subllm[chrome] driver needs it"
        return True, "ok"

    def check_guide(self) -> None:
        return None

    def run_text(self, prompt: str, *, timeout_s: float) -> WebTextResult:
        res = self._run(prompt, timeout_s, "text", None)
        return WebTextResult(
            text=res.get("text") or "",
            format=res.get("format") or "plain",
            served_model=res.get("served_model"),
            conversation_url=res["conversation_url"],
        )

    def run_image(self, prompt: str, dest: Path, *, timeout_s: float) -> ImageResult:
        return self._run(prompt, timeout_s, "image", dest)

    def _run(self, prompt: str, timeout_s: float, kind: str, dest: Path | None):
        prompt = normalize_prompt(prompt)
        if not prompt.strip():
            raise ClientError("the prompt is empty")
        with chatgpt_lock(timeout_s):
            tmpdir = Path(tempfile.mkdtemp(prefix="subllm-chatgpt-"))
            try:
                res = self._run_locked(prompt, timeout_s, kind, tmpdir)
                return res if kind == "text" else _collect_images(res, tmpdir, dest)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

    def _run_locked(self, prompt: str, timeout_s: float, kind: str, tmpdir: Path) -> dict:
        profile = profile_dir()
        stack = contextlib.ExitStack()
        try:
            profile.mkdir(parents=True, exist_ok=True)
            ctx = stack.enter_context(_open_context(profile))
        except Exception as e:
            stack.close()
            if re.search(r"profile|singleton|in use|existing browser session", str(e), re.I):
                raise ClientError(
                    f"cannot start Google Chrome with the subllm profile {profile} "
                    f"(another Chrome window may be using it): {e}"
                ) from e
            raise ClientError(f"cannot start Google Chrome: {e}") from e
        rec = _Rec()
        budget = _budget(timeout_s, prompt, kind)
        expired: list[str] = []

        def _fire() -> None:
            rec.dead = True
            expired.append(f"Chrome timed out after {budget:.0f}s")
            with contextlib.suppress(Exception):
                _kill_browser(profile)

        # a frozen renderer blocks evaluate forever; only killing the browser frees it
        timer = threading.Timer(budget, _fire)
        timer.daemon = True
        timer.start()
        try:
            try:
                result = _flow(ctx, prompt, timeout_s, kind, tmpdir, rec)
            except _Stop as s:
                result = _fail(rec, s.kind, s.detail)
            except Exception as e:
                result = _fail(rec, "client", f"{type(e).__name__}: {e}")
        finally:
            timer.cancel()
            with contextlib.suppress(Exception):
                stack.close()
        if not rec.sent and not result.get("ok") and result.get("kind") == "human_action":
            action = "solve the challenge" if "challenge" in result["detail"] else "log in to chatgpt.com"
            try:
                _spawn_login()
                how = f"{action} in the Chrome window that opened, close the window, then retry"
            except Exception as e:
                how = f"the Chrome window for logging in could not be opened ({e}); retry after logging in"
            result["detail"] = f"{result['detail']}; {how}"
        return classify(rec.events, [result], 0, rec.sent, None if result.get("ok") else (expired[0] if expired else None), "", "chrome")


def _fail(rec: _Rec, kind: str, detail: str) -> dict:
    return {
        "ok": False,
        "kind": kind,
        "detail": detail,
        "conversation_url": rec.url,
        "text": None,
        "served_model": None,
    }


def _flow(ctx, prompt: str, timeout_s: float, kind: str, tmpdir: Path, rec: _Rec) -> dict:
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    core = lambda name, *a, **kw: call_core(page, name, *a, **kw)  # noqa: E731
    is_chat = lambda: re.search(r"/c/", urlparse(page.url).path) is not None  # noqa: E731

    page.bring_to_front()
    try:
        page.goto(_HOME_URL, wait_until="domcontentloaded", timeout=15000)
    except Exception as e:
        # chatgpt.com can stay "interactive"; the state loop below is the real wait
        if type(e).__name__ != "TimeoutError":
            raise

    st = None
    last_err: Exception | None = None
    wait_end = _clock() + 45
    while _clock() < wait_end:
        try:
            st = core("state")
        except Exception as e:
            if rec.dead:
                raise
            last_err = e
            _sleep(0.5)
            continue
        if st.get("logged_in") or st.get("challenge"):
            break
        _sleep(0.5)
    if not st:
        raise _Stop("client", f"page state unavailable: {last_err}" if last_err else "page state unavailable")
    if st.get("challenge"):
        raise _Stop("human_action", "Cloudflare challenge")
    if not st.get("logged_in"):
        if st.get("login_prompt"):
            raise _Stop("human_action", "log in to chatgpt.com in Chrome")
        raise _Stop("client", "chatgpt.com did not become ready")
    if st.get("quota"):
        raise _Stop("quota", "usage limit reached")
    if st.get("mode_surface") != "chat":
        raise _Stop("client", "composer is in Work mode" if st.get("mode_surface") == "work" else "Chat mode not confirmed")
    if st.get("busy"):
        raise _Stop("client", "page is busy before the send")
    if is_chat():
        raise _Stop("client", "not a new chat")

    has_pm = page.evaluate("!!document.querySelector('div.ProseMirror[role=textbox]')")
    page.click("div.ProseMirror[role=textbox]" if has_pm else "#prompt-textarea")
    # Any key event can send early, so the record comes before the first one.
    rec.sent = True
    rec.events.append({"phase": "keypress"})
    parts = prompt.split("\n")
    for i, part in enumerate(parts):
        if part:
            page.keyboard.type(part, delay=40)
        if i < len(parts) - 1:
            page.keyboard.press("Shift+Enter")
    _sleep(0.4)
    if is_chat():
        raise _Stop("client", "chat opened before the send key; a send may have happened")
    if not core("composerMatches", prompt):
        raise _Stop("client", "composer text does not match the prompt")

    page.bring_to_front()
    page.keyboard.press("Enter")
    answer_deadline = _clock() + timeout_s

    acc_end = _clock() + 30
    acc = {"state": "pending"}
    while _clock() < acc_end:
        acc = core("acceptance")
        if acc.get("state") != "pending":
            break
        _sleep(0.25)
    if acc.get("state") == "rejected":
        rec.events.append({"phase": "rejected"})
        raise _Stop("client", "send rejected before the server accepted it")
    if acc.get("state") != "accepted":
        raise _Stop("client", "send not confirmed")
    rec.url = acc.get("conversation_url")
    rec.events.append({"phase": "accepted", "conversation_url": rec.url})

    image = kind == "image"
    turn_key = acc.get("turn_key")
    prev = None
    prev_images = None
    no_image_since = None
    ts: dict = {}
    while True:
        ts = core("turnStatus", turn_key, prev)
        err = ts.get("error_dialog")
        if err:
            raise _Stop("quota" if err.get("kind") == "quota" else "client", err.get("text") or "error dialog")
        if image:
            imgs = ts.get("images") or []
            if not ts.get("stop_visible") and imgs and imgs == prev_images:
                break
            if ts.get("done") and not imgs:
                if no_image_since is None:
                    no_image_since = _clock()
                if _clock() - no_image_since >= 8:
                    raise _Stop("output", "no image in the answer")
            else:
                no_image_since = None
            prev_images = imgs
        elif ts.get("done"):
            break
        if _clock() >= answer_deadline:
            raise _Stop("client", "timed out waiting for the answer")
        prev = ts.get("text")
        _sleep(0.7)

    cap = core("captureText", turn_key, main_world=True)
    if not image and not (cap and str(cap.get("text") or "").strip()):
        raise _Stop("output", "empty answer")
    served = core("servedModel", turn_key)
    images: list[str] = []
    if image:
        for i, src in enumerate(ts.get("images") or [], 1):
            b64 = page.evaluate(_FETCH_JS % json.dumps(src), isolated_context=False)
            path = tmpdir / f"img-{i}"
            path.write_bytes(base64.b64decode(b64, validate=True))
            images.append(str(path))
    return {
        "ok": True,
        "text": (cap or {}).get("text") or "",
        "format": "markdown" if (cap or {}).get("format") == "markdown" else "plain",
        "images": images,
        "served_model": served,
        "conversation_url": rec.url,
    }


def main(argv: list[str]) -> int:
    if argv != ["login"]:
        print("usage: python -m subllm.drivers.chatgpt_web.chrome login", file=sys.stderr)
        return 2
    profile = profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    with _open_context(profile) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        with contextlib.suppress(Exception):
            page.goto(_HOME_URL, wait_until="domcontentloaded", timeout=15000)
        with contextlib.suppress(Exception):
            # Chrome stays running on macOS after its last window closes; closing the context ends it
            page.wait_for_event("close", timeout=0)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
