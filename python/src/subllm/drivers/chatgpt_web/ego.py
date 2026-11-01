"""Drive chatgpt.com in an ego lite agent space through `ego-browser nodejs`.

One generated script per call. The ego runtime inherits no cwd or env, so every
input is inlined as a JSON literal. Outcome mapping follows the spec: once the
send key may have been pressed (the keypress record exists) nothing is retried.
"""
from __future__ import annotations

import contextlib
import json
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from subllm._images import save_numbered
from subllm.base import ImageResult, WebTextResult
from subllm.errors import (
    ClientError,
    HumanActionRequired,
    ModelDowngradeError,
    OutputError,
    QuotaError,
    SendUnconfirmedError,
)

_HERE = Path(__file__).parent
SUPPORTED_GUIDE_MAJOR = 2
_LINE_RE = re.compile(r"^SUBLLM_(EVENT|RESULT) (.*)$", re.MULTILINE)
_GUIDE_RE = re.compile(r"usage guide v(\d+)\.(\d+)\.(\d+)")


def lock_path() -> Path:
    return Path.home() / ".local/share/subllm/chatgpt-web.lock"


@contextlib.contextmanager
def chatgpt_lock(timeout_s: float):
    import fcntl

    path = lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(path, "a")
    except OSError as e:
        raise ClientError(f"cannot open the chatgpt-web lock {path}: {e}") from e
    try:
        end = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= end:
                    raise ClientError(f"another chatgpt-web call holds {path}") from None
                time.sleep(min(0.25, max(0.01, end - time.monotonic())))
        yield
    finally:
        fd.close()


def attach_timeout_s(n: int) -> float:
    """Bounded wait for n reference images to finish uploading to the composer."""
    return 60.0 + 15.0 * n if n else 0.0


def reference_paths(references: Sequence[str | Path]) -> list[str]:
    out = [str(r) for r in references]
    for r in out:
        if not Path(r).is_absolute():
            raise ClientError(f"reference image path is not absolute: {r}")
    return out


def normalize_prompt(prompt: str) -> str:
    # keyboard.type presses Enter for "\r" and moves focus for "\t"
    return prompt.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")


_MAGIC = ((b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"))


def _sniff(head: bytes) -> str | None:
    for magic, ext in _MAGIC:
        if head.startswith(magic):
            return ext
    return ".webp" if head[:4] == b"RIFF" and head[8:12] == b"WEBP" else None


def build_script(
    prompt: str,
    *,
    space: str,
    timeout_ms: int,
    keypress_file: str,
    kind: str = "text",
    image_dir: str | None = None,
    mode: str = "default",
    references: Sequence[str] = (),
) -> str:
    args = {
        "prompt": prompt,
        "space": space,
        "timeout_ms": timeout_ms,
        "keypress_file": keypress_file,
        "kind": kind,
        "image_dir": image_dir,
        "mode": mode,
        "references": list(references),
        "attach_timeout_ms": int(attach_timeout_s(len(references)) * 1000),
    }
    try:
        core = (_HERE / "page_core.js").read_text()
        template = (_HERE / "ego_run.js").read_text()
    except OSError as e:
        raise ClientError(f"cannot read the chatgpt-web script files: {e}") from e
    preamble = (
        f"const ARGS = {json.dumps(args)};\n"
        f"const KEYPRESS_FILE = {json.dumps(keypress_file)};\n"
        f"const CORE = {json.dumps(core)};\n"
    )
    return preamble + template


def parse_output(text: str) -> tuple[list[dict], list[dict]]:
    events: list[dict] = []
    results: list[dict] = []
    for kind, payload in _LINE_RE.findall(text):
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        if isinstance(obj, dict):
            (events if kind == "EVENT" else results).append(obj)
    return events, results


class EgoDriver:
    def __init__(self) -> None:
        self._available: tuple[bool, str] | None = None
        self._guide_ok = False

    def available(self) -> tuple[bool, str]:
        if self._available and self._available[0]:
            return self._available
        self._available = self._detect()
        return self._available

    def _detect(self) -> tuple[bool, str]:
        if not shutil.which("ego-browser"):
            return False, "ego-browser is not on PATH"
        try:
            v = subprocess.run(["ego-browser", "--version"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"ego-browser --version failed: {e}"
        if v.returncode != 0:
            return False, f"ego-browser --version exited {v.returncode}"
        token = f"SUBLLM_HEALTH_{secrets.token_hex(6)}"
        try:
            h = subprocess.run(
                ["ego-browser", "nodejs"],
                input=f'console.log("{token}");\n',
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"ego-browser health check failed: {e}"
        if token not in (h.stdout or "") + (h.stderr or ""):
            return False, "ego-browser health check did not run (is the ego app running?)"
        return True, "ok"

    def check_guide(self) -> None:
        if self._guide_ok:
            return
        try:
            r = subprocess.run(["ego-browser", "skill"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ClientError(f"ego-browser skill failed: {e}") from e
        m = _GUIDE_RE.search((r.stdout or "") + (r.stderr or ""))
        if not m:
            raise ClientError("could not read the ego-browser usage guide version; supported: 2.x")
        if int(m.group(1)) != SUPPORTED_GUIDE_MAJOR:
            raise ClientError(
                f"ego-browser usage guide v{m.group(1)}.{m.group(2)}.{m.group(3)} found; "
                f"supported: {SUPPORTED_GUIDE_MAJOR}.x"
            )
        self._guide_ok = True

    def run_text(self, prompt: str, *, timeout_s: float, mode: str = "default") -> WebTextResult:
        res = self._run(prompt, timeout_s, "text", None, mode)
        return WebTextResult(
            text=res.get("text") or "",
            format=res.get("format") or "plain",
            served_model=res.get("served_model"),
            conversation_url=res["conversation_url"],
        )

    def run_image(
        self,
        prompt: str,
        dest: Path,
        *,
        timeout_s: float,
        mode: str = "default",
        references: Sequence[str | Path] = (),
    ) -> ImageResult:
        return self._run(prompt, timeout_s, "image", dest, mode, reference_paths(references))

    def _run(
        self,
        prompt: str,
        timeout_s: float,
        kind: str,
        dest: Path | None,
        mode: str = "default",
        references: Sequence[str] = (),
    ):
        prompt = normalize_prompt(prompt)
        if not prompt.strip():
            raise ClientError("the prompt is empty")
        with chatgpt_lock(timeout_s):
            return self._run_locked(prompt, timeout_s, kind, dest, mode, references)

    def _run_locked(
        self,
        prompt: str,
        timeout_s: float,
        kind: str,
        dest: Path | None,
        mode: str = "default",
        references: Sequence[str] = (),
    ):
        tmpdir = Path(tempfile.mkdtemp(prefix="subllm-chatgpt-"))
        keypress = tmpdir / "keypress"
        try:
            script = build_script(
                prompt,
                space=f"subllm-chatgpt-{secrets.token_hex(4)}",
                timeout_ms=int(timeout_s * 1000),
                keypress_file=str(keypress),
                kind=kind,
                image_dir=str(tmpdir),
                mode=mode,
                references=references,
            )
            budget = (
                timeout_s + 120 + int(len(prompt) * 0.06) + (60 if kind == "image" else 0)
                + attach_timeout_s(len(references))
            )
            out = err = ""
            code: int | None = None
            expired: str | None = None
            try:
                p = subprocess.run(
                    ["ego-browser", "nodejs"], input=script, capture_output=True, text=True, timeout=budget
                )
                out, err, code = p.stdout or "", p.stderr or "", p.returncode
            except FileNotFoundError as e:
                raise ClientError("ego-browser is not installed") from e
            except subprocess.TimeoutExpired as e:
                expired = f"ego-browser nodejs timed out after {budget:.0f}s"
                out = _txt(e.stdout)
                err = _txt(e.stderr)
            sent = keypress.exists()
            res = _map(out, err, code, sent, expired)
            return res if kind == "text" else _collect_images(res, tmpdir, dest)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _collect_images(res: dict, tmpdir: Path, dest: Path) -> ImageResult:
    url = res["conversation_url"]
    listed = res.get("images")
    if not isinstance(listed, list) or not listed:
        raise SendUnconfirmedError("chatgpt.com reported success but listed no image", conversation_url=url)
    root = tmpdir.resolve()
    files: list[tuple[Path, str]] = []
    for item in listed:
        p = Path(item) if isinstance(item, str) else None
        ext = None
        if p is not None and not p.is_symlink() and p.is_file() and p.parent.resolve() == root:
            try:
                with open(p, "rb") as f:
                    ext = _sniff(f.read(12))
            except OSError:
                pass
        if ext is None:
            raise SendUnconfirmedError(
                "chatgpt.com reported an image that is missing or not a PNG/WebP/JPEG file", conversation_url=url
            )
        files.append((p, ext))
    try:
        paths = save_numbered(files, dest)
    except ClientError as e:
        raise SendUnconfirmedError(f"image generated but not saved: {e}", conversation_url=url) from e
    return ImageResult(
        paths=paths,
        route="web",
        text=res.get("text") or "",
        conversation_url=url,
        served_model=res.get("served_model"),
    )


def _txt(v) -> str:
    if v is None:
        return ""
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else v


_NAMES = {"ego": "ego-browser", "chrome": "chrome driver"}


def _map(out: str, err: str, code: int | None, sent: bool, expired: str | None) -> dict:
    events, results = parse_output(out + "\n" + err)
    return classify(events, results, code, sent, expired, err.strip()[-400:], "ego")


def classify(
    events: list[dict],
    results: list[dict],
    code: int | None,
    sent: bool,
    expired: str | None,
    tail: str,
    browser: str,
) -> dict:
    res = results[0] if len(results) == 1 else None
    url = None
    for ev in events:
        if ev.get("phase") == "accepted" and ev.get("conversation_url"):
            url = ev["conversation_url"]
    if url is None and res:
        url = res.get("conversation_url")

    if not sent:
        if expired:
            raise ClientError(expired)
        if res is None:
            raise ClientError(f"{_NAMES[browser]} produced no result: {tail}")
        if res.get("ok"):
            raise ClientError(f"{_NAMES[browser]} reported success but no send was recorded")
        kind, detail = res.get("kind"), res.get("detail") or ""
        if kind == "human_action":
            raise HumanActionRequired(
                f"chatgpt.com needs a person: {detail}", browser=browser, detail=detail
            )
        if kind == "quota":
            raise QuotaError(f"chatgpt.com usage limit: {detail}")
        raise ClientError(detail or "chatgpt-web failed before the send")

    if expired or res is None:
        raise SendUnconfirmedError(
            expired or f"send key pressed but no result was reported: {tail}", conversation_url=url
        )
    if res.get("ok"):
        if code == 0:
            return {**res, "conversation_url": url}
        raise SendUnconfirmedError(
            f"{_NAMES[browser]} exited {code} after reporting success", conversation_url=url
        )
    kind, detail = res.get("kind"), res.get("detail") or ""
    rejected = any(ev.get("phase") == "rejected" for ev in events)
    if rejected and kind == "client":
        raise ClientError(f"send rejected; not resent: {detail}")
    if kind == "output":
        raise OutputError(detail or "empty answer")
    if kind == "downgrade":
        raise ModelDowngradeError(
            detail or "model downgrade",
            text=res.get("text") or "",
            served_model=res.get("served_model"),
            requested_mode=res.get("requested_mode"),
        )
    raise SendUnconfirmedError(
        f"send key pressed; outcome unconfirmed ({kind}): {detail}", conversation_url=url
    )
