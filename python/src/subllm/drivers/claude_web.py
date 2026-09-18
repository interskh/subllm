"""Web-agent mode for interactive `claude` over tmux (D-3, D-3a, D-4, D-4b,
D-4c, D-6).

Separate from `run_claude_tmux`, whose behaviour is unchanged: this entry point
runs ONE browsing turn (D-4a, no retry) with

- a web-only (or empty) tool set and no MCP servers, settings or slash commands;
- a dedicated, auth-only `CLAUDE_CONFIG_DIR` seeded from the owner's keychain
  credential and deleted afterwards — never the owner's real home;
- an empty temporary working directory, and an empty `HOME`;
- a dedicated tmux server started from the D-6 allowlist, so no environment of
  an already-running server (and none of the caller's secrets) can leak in;
- every connection forced through `EgressFilterProxy`, with the client confined
  by `sandbox-exec` so that filter is its only TCP destination (D-4b: the
  public-target invariant is enforced BEFORE each connection, not after).

The filter is a network-layer CONNECT filter: it terminates no TLS, reads no
token and rewrites no request, so it is not the subscription-to-API proxy
AGENTS.md forbids — the client talks to Anthropic itself, natively.

Flags and shapes are the measured set for claude 2.1.276 and 2.1.277, the two
admitted versions (the committed fixtures are 2.1.276 captures; see
python/tests/fixtures/claude-2.1.276-*.jsonl).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid as uuid_mod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

from subllm.drivers.claude_trace import ParsedTranscript, parse_transcript
from subllm.drivers.claude_tmux import (
    _BANNER_MARKERS,
    _PASTE_PLACEHOLDER_RE,
    _TRUST_DIALOG_MARKER,
    _project_jsonl_path,
    _q,
    _quota_banner,
    _turn_complete,
)
from subllm.drivers.egress import (
    CODE_PORT,
    CODE_RESOLVED_NON_PUBLIC,
    CODE_UNRESOLVED,
    Attempt,
    EgressFilterProxy,
    confined_argv,
)
from subllm.errors import ClientError, IsolationError, QuotaError
from subllm.publictargets import (
    CachingResolver,
    Resolver,
    classify_url,
    default_resolver,
)
from subllm.webagent import TraceEvent

#: D-6: the child — and the tmux SERVER that owns it — get these and nothing
#: else. HOME points at an empty temp dir; CLAUDE_CONFIG_DIR at the throwaway
#: auth-only home; the proxy variables at the egress filter (D-4b).
ENV_ALLOWLIST = ("PATH", "HOME", "USER", "LANG", "TERM", "CLAUDE_CONFIG_DIR")
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")

#: D-3: the tool set web-agent mode offers. `--tools ''` offers none (D-4c).
ALLOWED_TOOLS = ("WebSearch", "WebFetch")

#: D-3a, admitted-version branch: this client reports no tool inventory BEFORE
#: the model acts — the `prompt_snapshot` attachment that names the offered
#: tools is written with the request and can only be read afterwards (the
#: parser checks it as a second layer), so admission is by recorded evidence: a
#: version runs only after it has passed the live G3 negative control. A client
#: update blocks web-agent mode until that control is re-run — which happened
#: during this task: the client auto-updated mid-slice and the gate stopped the
#: call, as designed.
#:   2.1.276 — the committed fixtures are its captures; live G1 + G3 green.
#:   2.1.277 — live G1 + G3 re-run on it (grounded answer; loopback refused
#:             pre-connection with zero requests at the origin; no leak), and
#:             the parser needed no change, so the transcript shape is unchanged.
ADMITTED_VERSIONS = frozenset({"2.1.276", "2.1.277"})

#: The keychain item holding the owner's OAuth credential for the DEFAULT config
#: home. The item is namespaced per config home, so a fresh CLAUDE_CONFIG_DIR is
#: "Not logged in" until this blob's `claudeAiOauth` key is copied into it.
_KEYCHAIN_SERVICE = "Claude Code-credentials"
#: Only this key is copied. The blob also holds `mcpOAuth` server secrets, which
#: an auth-only home must not carry (D-3).
_CREDENTIAL_KEY = "claudeAiOauth"

#: Dialog wordings that must never be answered blindly: the workspace-trust
#: dialog defaults to "No, exit", and its own selection cursor is one of the
#: banner markers, so a readiness wait that missed it would either hang or
#: answer it wrong. Seeding the projects entry skips it; seeing it means the
#: seed did not take, which is a loud failure (fact 2).
_DIALOG_MARKERS = (
    "Quick safety check",
    "Is this a project you created or one you trust",
    _TRUST_DIALOG_MARKER,
)

#: Pane wordings that mean the dedicated home did not authenticate. They match
#: the banner markers too, so without this the readiness wait would "succeed",
#: the prompt would go to a login menu and the call would burn its whole timeout
#: before reporting "turn did not complete" — the wrong cause.
#: `/login` alone is NOT a marker: an AUTHENTICATED session shows "Your login
#: expires in 1 day · run /login to renew" (measured live, and it made this
#: check fail a working run). Only wordings that state the session has no
#: credential count.
_LOGIN_MARKERS = (
    "Not logged in",
    "Select login method",
    "Log in with your Claude account",
    "Invalid API key",
)

#: D-3: `--safe-mode` disables the owner's customizations but its own help says
#: "Admin-managed (policy) settings still apply", and a managed
#: `PreToolUse` hook would run a shell inside the session with the allowlisted
#: environment, leaving nothing the transcript parser classifies. None of these
#: exists on this machine (measured), and if one appears the call fails closed
#: rather than running under a policy nobody read.
MANAGED_SETTINGS_PATHS = (
    "/Library/Application Support/ClaudeCode/managed-settings.json",
    "/etc/claude-code/managed-settings.json",
)

_READY_TIMEOUT_S = 60.0
#: The transcript is read only once it has stopped growing: the client writes
#: trailing records (tool results, attachments) after the assistant's end_turn.
_SETTLE_QUIET_S = 1.5
_SETTLE_LIMIT_S = 15.0


@dataclass(frozen=True)
class ClaudeWebRun:
    answer: str
    trace: tuple[TraceEvent, ...]
    trace_complete: bool
    client_version: str
    duration_ms: int


@dataclass
class _Turn:
    """Everything one live turn produced, before any judgement."""

    records: list[dict[str, Any]]
    #: the dedicated tmux SERVER's global environment, read back from it
    server_env: dict[str, str]
    #: the empty working directory the turn ran in (deleted by then)
    work_dir: str
    extra_transcripts: list[str]
    subagent_dirs: list[str]
    attempts: tuple[Attempt, ...]
    duration_ms: int
    version: str


def run_claude_web_task(
    prompt: str,
    *,
    model: str,
    tools: Literal["web", "none"] = "web",
    timeout_s: int = 300,
    resolve: Resolver | None = None,
    credential_source: Callable[[], str] | None = None,
    poll_interval_s: float = 1.0,
) -> ClaudeWebRun:
    """One isolated browsing (or no-tool) turn. Raises ClientError before the
    agent runs when the client version is not admitted, IsolationError (carrying
    the trace, never the answer) on any D-5 / D-4b violation, QuotaError on a
    subscription limit."""
    turn = run_turn(
        prompt,
        model=model,
        tools=tools,
        timeout_s=timeout_s,
        resolve=resolve,
        credential_source=credential_source,
        poll_interval_s=poll_interval_s,
    )
    return judge_turn(turn, tools=tools, model=model, resolve=resolve)


def run_turn(
    prompt: str,
    *,
    model: str,
    tools: Literal["web", "none"] = "web",
    timeout_s: int = 300,
    resolve: Resolver | None = None,
    credential_source: Callable[[], str] | None = None,
    poll_interval_s: float = 1.0,
) -> _Turn:
    """Run the turn and return its raw evidence. Split from the judgement so a
    live capture can be recorded (and committed as a fixture) without a parser
    that has not been written against it yet."""
    if tools not in ("web", "none"):
        raise ClientError(f"tools must be 'web' or 'none', got {tools!r}")
    if not model:
        raise ClientError("web-agent mode requires an explicit model")

    _check_managed_settings()
    # D-3a, TOCTOU: resolve the binary ONCE and run the concrete versioned
    # executable. `claude` on PATH is a symlink the client rewrites when it
    # auto-updates (it did so mid-development), so checking `claude --version`
    # and then launching `claude` can admit one build and run another — and the
    # transcript cross-check only notices after the model has acted.
    binary = _resolve_binary()

    session_id = str(uuid_mod.uuid4())
    socket_name = f"subllm-web-{session_id[:8]}"
    work_dir = tempfile.mkdtemp(prefix="subllm-claude-cwd-")
    config_dir = tempfile.mkdtemp(prefix="subllm-claude-cfg-")
    fake_home = tempfile.mkdtemp(prefix="subllm-claude-home-")
    try:
        _seed_config_home(
            config_dir, work_dir, credential_source or _keychain_credential
        )
        version = _client_version(binary, _child_env(config_dir, fake_home, None))
        if version not in ADMITTED_VERSIONS:
            raise ClientError(
                f"claude web-agent mode is not admitted for client {version!r}: "
                f"this client reports no tool inventory, so a version runs only "
                f"after it passes the live G3 negative control (D-3a). Re-run "
                f"that control and add the version."
            )

        with EgressFilterProxy(resolve=resolve or default_resolver) as proxy:
            env = _child_env(config_dir, fake_home, proxy.proxy_url)
            argv = _claude_argv(binary, session_id, model, tools)
            started = time.monotonic()
            with confined_argv(proxy.port, argv) as confined:
                command = " ".join(_q(a) for a in confined)
                _start_server(socket_name, work_dir, command, env)
                try:
                    _wait_for_ready(socket_name)
                    _send_prompt(socket_name, prompt)
                    records = _await_transcript(
                        socket_name,
                        _project_jsonl_path(config_dir, work_dir, session_id),
                        timeout_s=timeout_s,
                        poll_interval_s=poll_interval_s,
                    )
                    # D-6: read the SERVER's own global environment back before
                    # killing it. The env we started it with is what we control;
                    # this is what it actually holds (a config file or a client
                    # could have added to it), and it is the live G3 evidence.
                    server_env = _server_env(socket_name)
                finally:
                    _kill_server(socket_name)
            duration_ms = int((time.monotonic() - started) * 1000)
        extra, subagents = _sibling_sessions(config_dir, work_dir, session_id)
        return _Turn(
            records=records,
            server_env=server_env,
            work_dir=work_dir,
            extra_transcripts=extra,
            subagent_dirs=subagents,
            attempts=proxy.attempts,
            duration_ms=duration_ms,
            version=version,
        )
    finally:
        for path in (config_dir, work_dir, fake_home):
            shutil.rmtree(path, ignore_errors=True)


def judge_turn(
    turn: _Turn,
    *,
    tools: Literal["web", "none"],
    model: str,
    resolve: Resolver | None = None,
) -> ClaudeWebRun:
    """Parse the transcript and apply D-3a / D-4b / D-5 to it."""
    _check_server_env(turn.server_env, turn.work_dir)
    parsed = parse_transcript(turn.records, tools=tools)
    _attach_attempts(parsed, turn.attempts)

    if turn.subagent_dirs or turn.extra_transcripts:
        # A spawned sub-agent writes its own transcript beside ours (mirrors
        # slice 1's one-rollout rule): the turn was not alone.
        raise IsolationError(
            "sub-agent transcripts beside the session transcript: "
            + ", ".join(sorted(turn.subagent_dirs + turn.extra_transcripts)),
            parsed.events,
        )
    if parsed.violations:
        raise IsolationError(
            "claude web-agent isolation violation: " + "; ".join(parsed.violations),
            parsed.events,
        )
    _check_refusals(parsed, turn.attempts)
    _check_targets(parsed, resolve=resolve)

    if parsed.client_version and parsed.client_version != turn.version:
        raise ClientError(
            f"transcript reports client version {parsed.client_version!r}, but "
            f"{turn.version!r} was admitted"
        )
    if parsed.model and parsed.model != model:
        raise ClientError(
            f"transcript ran model {parsed.model!r}, but {model!r} was requested"
        )
    if parsed.answer is None:
        raise ClientError("no completed text-bearing assistant turn in transcript")

    return ClaudeWebRun(
        answer=parsed.answer,
        trace=tuple(parsed.events),
        trace_complete=parsed.trace_complete,
        client_version=parsed.client_version or turn.version,
        duration_ms=turn.duration_ms,
    )


# --- configuration home (D-3) ----------------------------------------------


def _keychain_credential() -> str:
    """The owner's credential blob, read from the macOS keychain."""
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise ClientError(f"could not read the claude credential: {e}") from e
    if proc.returncode != 0:
        raise ClientError(
            "no claude credential in the keychain "
            f"({(proc.stderr or '').strip()[:120]}) — log in with `claude` first"
        )
    return proc.stdout


def _seed_config_home(
    config_dir: str, work_dir: str, credential_source: Callable[[], str]
) -> None:
    """Write the ONLY two files the dedicated home needs: the OAuth credential
    and enough onboarding state to skip the theme wizard and the workspace-trust
    dialog. Nothing of the owner's settings, memory, skills, agents, plugins,
    history or MCP servers is copied."""
    try:
        blob = json.loads(credential_source() or "")
    except (json.JSONDecodeError, TypeError) as e:
        raise ClientError(f"the claude credential is not JSON: {e}") from e
    if not isinstance(blob, dict) or _CREDENTIAL_KEY not in blob:
        raise ClientError(
            f"the claude credential has no {_CREDENTIAL_KEY!r} key; web-agent "
            f"mode cannot authenticate from a dedicated configuration home"
        )
    # ONLY claudeAiOauth: the blob also carries `mcpOAuth` server secrets, and
    # an auth-only home must not hold them (D-3).
    credentials = Path(config_dir) / ".credentials.json"
    credentials.write_text(json.dumps({_CREDENTIAL_KEY: blob[_CREDENTIAL_KEY]}))
    credentials.chmod(0o600)

    resolved = str(Path(work_dir).resolve())
    (Path(config_dir) / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "theme": "dark",
                "bypassPermissionsModeAccepted": True,
                "firstStartTime": "2026-01-01T00:00:00.000Z",
                # Keyed by the RESOLVED path, which is what the client compares
                # (and what the transcript path encodes).
                "projects": {
                    resolved: {
                        "hasTrustDialogAccepted": True,
                        "hasCompletedProjectOnboarding": True,
                        "allowedTools": [],
                        "mcpServers": {},
                        "enabledMcpjsonServers": [],
                        "disabledMcpjsonServers": [],
                        "hasClaudeMdExternalIncludesApproved": False,
                        "hasClaudeMdExternalIncludesWarningShown": True,
                    }
                },
            }
        )
    )


def _sibling_sessions(
    config_dir: str, work_dir: str, session_id: str
) -> tuple[list[str], list[str]]:
    """Transcripts other than ours in the project dir, and any sub-agent
    transcript directory. Read before the home is deleted."""
    project_dir = _project_jsonl_path(config_dir, work_dir, session_id).parent
    extra: list[str] = []
    subagents: list[str] = []
    if not project_dir.is_dir():
        return extra, subagents
    for entry in sorted(project_dir.iterdir()):
        if entry.is_dir():
            subagents.append(entry.name)
        elif entry.suffix == ".jsonl" and entry.stem != session_id:
            extra.append(entry.name)
    return extra, subagents


# --- child environment + argv (D-6, D-3) -----------------------------------


def _child_env(
    config_dir: str, home: str, proxy_url: str | None
) -> dict[str, str]:
    """D-6: an explicit allowlist, built from scratch. Nothing else in the
    caller's environment reaches the child — and the caller's NO_PROXY is never
    forwarded, so it cannot punch a hole in the egress filter."""
    env: dict[str, str] = {}
    for name in ENV_ALLOWLIST:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    env["HOME"] = home
    env["CLAUDE_CONFIG_DIR"] = config_dir
    env.setdefault("PATH", os.defpath)
    if proxy_url is not None:
        for name in PROXY_VARS:
            env[name] = proxy_url
    return env


def _claude_argv(binary: str, session_id: str, model: str, tools: str) -> list[str]:
    """Measured on 2.1.276. `--restricted` is NOT used: it refuses
    bypassPermissions, which D-3 keeps. `--bare` is NOT used: it never reads the
    OAuth credential, so the subscription cannot authenticate."""
    return [
        binary,
        "--session-id", session_id,
        "--permission-mode", "bypassPermissions",
        "--model", model,
        # '' offers no tool at all (D-4c).
        "--tools", ",".join(ALLOWED_TOOLS) if tools == "web" else "",
        "--safe-mode",
        "--strict-mcp-config",
        "--disable-slash-commands",
    ]


def _resolve_binary() -> str:
    """The concrete executable `claude` points at right now. What is version-
    checked and what is executed must be the same file (D-3a)."""
    found = shutil.which("claude")
    if found is None:
        raise ClientError("claude binary not found on PATH")
    return os.path.realpath(found)


def _check_managed_settings() -> None:
    for path in MANAGED_SETTINGS_PATHS:
        if os.path.exists(path):
            raise ClientError(
                f"admin-managed settings exist at {path}: they apply even under "
                f"--safe-mode, so a managed hook could run a shell inside the "
                f"session (D-3). Web-agent mode refuses to run until it is "
                f"reviewed."
            )


def _client_version(binary: str, env: dict[str, str]) -> str:
    try:
        proc = subprocess.run(
            [binary, "--version"],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as e:
        raise ClientError("claude binary not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise ClientError("claude --version timed out") from e
    if proc.returncode != 0:
        raise ClientError(f"claude --version failed: {(proc.stderr or '')[:200]}")
    text = (proc.stdout or "").strip()
    return text.split()[0] if text else ""


# --- dedicated tmux server (D-6) -------------------------------------------


def _tmux(socket_name: str, *args: str, timeout: float = 5.0):
    """Every call names the dedicated server. `-f /dev/null` keeps the owner's
    ~/.tmux.conf out: it could `set-environment -g` or `run-shell` into the
    server we just started from the allowlist."""
    try:
        return subprocess.run(
            ["tmux", "-L", socket_name, "-f", "/dev/null", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise ClientError("tmux binary not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise ClientError(f"tmux command timed out: {' '.join(args)}") from e


def _start_server(
    socket_name: str, work_dir: str, command: str, env: dict[str, str]
) -> None:
    """D-6: the SERVER itself is started from the allowlist. `-e` on
    new-session alone is not enough — a session inherits the global environment
    of whichever server is already running, so web-agent mode never joins one."""
    try:
        res = subprocess.run(
            [
                "tmux", "-L", socket_name, "-f", "/dev/null",
                "new-session", "-d", "-s", "web", "-x", "200", "-y", "50",
                "-c", work_dir, command,
            ],
            env=env,
            # tmux puts its OWN working directory into the server's global
            # environment as PWD (measured live): start it inside the empty work
            # dir so that name cannot carry the caller's path.
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as e:
        raise ClientError("tmux binary not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise ClientError("tmux new-session timed out") from e
    if res.returncode != 0:
        raise ClientError(f"tmux new-session failed: {res.stderr.strip()[:200]}")


#: Names the platform or tmux injects, rather than passing through from the
#: caller: `__CF_USER_TEXT_ENCODING` is added by macOS to every spawned process
#: (a uid + encoding pair), and tmux sets `PWD` to the session's own `-c`
#: working directory into the server's global environment as `PWD`, which is
#: why the server is started inside the empty work dir and why `PWD` is checked
#: against it rather than merely allowed.
_OS_INJECTED = ("__CF_USER_TEXT_ENCODING", "PWD")


def _server_env(socket_name: str) -> dict[str, str]:
    """The dedicated server's global environment, as tmux reports it."""
    res = _tmux(socket_name, "show-environment", "-g", timeout=10)
    if res.returncode != 0:
        raise ClientError(
            f"could not read the tmux server environment: "
            f"{(res.stderr or '').strip()[:200]}"
        )
    env: dict[str, str] = {}
    for line in (res.stdout or "").splitlines():
        if not line or line.startswith("-"):  # '-NAME' = removed by tmux
            continue
        name, _, value = line.partition("=")
        env[name] = value
    return env


def _check_server_env(server_env: dict[str, str], work_dir: str = "") -> None:
    """D-6, checked on the process that ACTUALLY ran the agent: the dedicated
    server must hold the allowlist and nothing else. A leaked variable stops the
    call rather than being reported afterwards."""
    allowed = set(ENV_ALLOWLIST) | set(PROXY_VARS) | set(_OS_INJECTED)
    extra = sorted(set(server_env) - allowed)
    if extra:
        raise ClientError(
            f"the dedicated tmux server carries variables outside the D-6 "
            f"allowlist: {extra}"
        )
    pwd = server_env.get("PWD")
    if work_dir and pwd is not None and pwd not in (
        work_dir, str(Path(work_dir).resolve())
    ):
        raise ClientError(
            f"the dedicated tmux server reports PWD={pwd!r}, which is not the "
            f"empty working directory this call created"
        )


def _kill_server(socket_name: str) -> None:
    try:
        _tmux(socket_name, "kill-server", timeout=10)
    except ClientError:
        pass  # cleanup failure must never mask the turn's real result/error


def _capture(socket_name: str) -> str:
    return _tmux(socket_name, "capture-pane", "-p", "-t", "web").stdout


def _wait_for_ready(socket_name: str, timeout_s: float = _READY_TIMEOUT_S) -> None:
    """Wait for the TUI input prompt. A dialog is a LOUD failure, never
    answered: the seeded projects entry is what skips the workspace-trust
    dialog, whose default option is "No, exit" — seeing it means the seed did
    not take, and blindly sending a key would either exit or accept something we
    did not read."""
    deadline = time.monotonic() + timeout_s
    pane = ""
    while time.monotonic() < deadline:
        pane = _capture(socket_name)
        if _quota_banner(pane):
            raise QuotaError(f"claude limit banner: {pane.strip()[:200]}")
        login = next((m for m in _LOGIN_MARKERS if m in pane), None)
        if login is not None:
            raise ClientError(
                f"claude is not authenticated in the dedicated configuration "
                f"home (pane shows {login!r}): the credential seed did not "
                f"take — check the keychain item and re-run. Pane: "
                f"{pane.strip()[:300]}"
            )
        marker = next((m for m in _DIALOG_MARKERS if m in pane), None)
        if marker is not None:
            raise ClientError(
                f"claude showed a dialog ({marker!r}) instead of the input "
                f"prompt; the dedicated configuration home was not seeded as "
                f"this client expects: {pane.strip()[:400]}"
            )
        if any(m in pane for m in _BANNER_MARKERS):
            return
        time.sleep(0.5)
    raise ClientError(
        f"claude TUI prompt did not appear within {timeout_s}s; last pane: "
        f"{pane.strip()[:400]}"
    )


#: Characters that put this TUI into a different input mode instead of sending
#: text: `!` is bash mode (a shell command, MEASURED to execute locally and to
#: be reachable through BOTH send paths) and `/` is a slash command. The prompt
#: is caller-supplied, so this must not rest on caller discipline.
_MODE_PREFIXES = ("!", "/")

#: C0 controls and DEL, minus tab and newline. `send-keys -l` delivers these
#: literally, so an embedded bracketed-paste sequence
#: (`ESC [ 200 ~ !id ESC [ 201 ~`) would open bash mode from the MIDDLE of a
#: prompt that starts with neither `!` nor `/`. Task text has no legitimate use
#: for raw control bytes, so they are removed before anything else is decided.
_CONTROL_BYTES = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def safe_prompt(prompt: str) -> str:
    """Make a caller-supplied prompt safe to type into the TUI.

    Two rules, in order: strip control/escape bytes (which can smuggle a
    bracketed-paste sequence and reach bash mode from mid-prompt), then prefix a
    single space if what remains would switch input mode. Neither changes what
    the task text asks for, whereas refusing the prompt would fail legitimate
    tasks that open with `!` or `/`."""
    prompt = _CONTROL_BYTES.sub("", prompt)
    return " " + prompt if prompt.startswith(_MODE_PREFIXES) else prompt


def _send_prompt(socket_name: str, prompt: str) -> None:
    prompt = safe_prompt(prompt)
    if "\n" in prompt or len(prompt.encode("utf-8")) > 4096:
        buf = f"subllm-{uuid_mod.uuid4().hex[:8]}"
        try:
            subprocess.run(
                ["tmux", "-L", socket_name, "-f", "/dev/null",
                 "load-buffer", "-b", buf, "-"],
                input=prompt, text=True, check=True, timeout=10,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            raise ClientError(f"tmux load-buffer failed: {e}") from e
        _tmux(socket_name, "paste-buffer", "-p", "-d", "-b", buf, "-t", "web")
        _wait_for_paste_placeholder(socket_name)
    else:
        _tmux(socket_name, "send-keys", "-t", "web", "-l", prompt)
        time.sleep(0.3)
    _tmux(socket_name, "send-keys", "-t", "web", "Enter")


def _wait_for_paste_placeholder(socket_name: str, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _PASTE_PLACEHOLDER_RE.search(_capture(socket_name)):
            return
        time.sleep(0.1)
    # don't raise: pressing Enter anyway is cheaper than a failed turn


def _await_transcript(
    socket_name: str,
    jsonl_path: Path,
    *,
    timeout_s: int,
    poll_interval_s: float,
) -> list[dict[str, Any]]:
    """Poll for the completed turn, then wait for the transcript to stop growing
    before reading it: the client writes trailing records (tool results,
    attachments) after the assistant's end_turn."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if jsonl_path.exists():
            lines = jsonl_path.read_text(errors="replace").splitlines()
            if _turn_complete(lines):
                return _read_records(_settle(jsonl_path))
        if _quota_banner(_capture(socket_name)):
            raise QuotaError("claude limit banner during generation")
        time.sleep(poll_interval_s)
    raise ClientError(f"claude turn did not complete within {timeout_s}s")


def _settle(jsonl_path: Path) -> Path:
    quiet_s, limit_s = _SETTLE_QUIET_S, _SETTLE_LIMIT_S
    deadline = time.monotonic() + limit_s
    size = -1
    while time.monotonic() < deadline:
        current = jsonl_path.stat().st_size
        if current == size:
            break
        size = current
        time.sleep(quiet_s)
    return jsonl_path


def _read_records(jsonl_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in jsonl_path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            records.append({"type": "<unparsable>", "line": line})
            continue
        records.append(record if isinstance(record, dict) else {"type": "<unparsable>"})
    return records


# --- D-4b ------------------------------------------------------------------


def _host_of(url: str | None) -> str | None:
    if not url:
        return None
    return (urlsplit(url).hostname or "").strip().rstrip(".").lower() or None


def _attach_attempts(parsed: ParsedTranscript, attempts: tuple[Attempt, ...]) -> None:
    """D-4 "raw evidence is preserved": what the egress filter did with a fetch's
    host travels in that fetch event's raw record, so an isolation failure — in
    particular a REFUSAL, where the client reports only an error — can be
    diagnosed from the result itself."""
    by_host: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        host = (attempt.host or "").strip().rstrip(".").lower()
        if host:
            by_host.setdefault(host, []).append(_attempt_json(attempt))
    for i, event in enumerate(parsed.events):
        if event.kind != "fetch":
            continue
        hosts = {h for h in (_host_of(event.url), _host_of(event.final_url)) if h}
        matched = [a for h in sorted(hosts) for a in by_host.get(h, [])]
        if not matched:
            continue
        raw = dict(event.raw)
        raw["egress_attempts"] = matched
        parsed.events[i] = _replace_raw(event, raw)


def _replace_raw(event: TraceEvent, raw: dict[str, Any]) -> TraceEvent:
    return TraceEvent(
        kind=event.kind,
        raw_type=event.raw_type,
        raw=raw,
        query=event.query,
        url=event.url,
        final_url=event.final_url,
        ok=event.ok,
        result_urls=event.result_urls,
    )


def _attempt_json(attempt: Attempt) -> dict[str, Any]:
    return {
        "at": attempt.at,
        "method": attempt.method,
        "target": attempt.target,
        "host": attempt.host,
        "port": attempt.port,
        "address": attempt.address,
        "allowed": attempt.allowed,
        "reason": attempt.reason,
        "connected": attempt.connected,
        "code": attempt.code,
    }


def _check_refusals(
    parsed: ParsedTranscript, attempts: tuple[Attempt, ...]
) -> None:
    """Judge every destination the filter REFUSED, on its machine-readable
    `code` — never on the prose.

    The filter's recorded verdict is the authoritative D-4b evidence: it is what
    the name resolved to at connection time. A refusal is NOT skipped because
    the host also appears in the trace — letting `_check_targets` re-resolve it
    later would return an answer whenever the name moved back to a public
    address between the fetch and the judgement (DNS rebinding, a TTL rolling to
    a CDN), with the loopback verdict sitting unused in the raw record (D-5).

    Three outcomes:
    - `port`: the target was already judged PUBLIC (publicness is checked before
      the port rule), so this is web-agent mode's own 80/443 policy, not
      evidence of a non-public destination. Not a violation.
    - `unresolved` or `resolved_non_public` on a destination NO traced fetch
      asked for (a client-internal call: an api.anthropic.com retry, telemetry,
      the marketplace): the owner's own DNS decides both — a miss, or a sinkhole
      answering 0.0.0.0 — and nothing connected. That is a delivery failure, so
      it is a `ClientError` the caller may retry, not an `IsolationError` the
      consumer is told never to retry.
    - anything else (`non_public` by LITERAL, `scheme`, `unparsable`, `missing`,
      or a code this driver does not know): `IsolationError` whether traced or
      not — nothing legitimate dials a private address literal through this
      filter, so it is evidence of a hidden tool or client behaviour.
    - a TRACED fetch keeps `IsolationError` for every non-port code, including
      both non-public forms: that is the D-4b case and does not soften.
    """
    traced = {
        h
        for event in parsed.events
        if event.kind == "fetch"
        for h in (_host_of(event.url), _host_of(event.final_url))
        if h
    }
    for attempt in attempts:
        if attempt.allowed or attempt.code == CODE_PORT:
            continue
        host = (attempt.host or "").strip().rstrip(".").lower()
        asked_for = host in traced
        if not asked_for and attempt.code in (
            CODE_UNRESOLVED, CODE_RESOLVED_NON_PUBLIC
        ):
            raise ClientError(
                f"the egress filter refused the client-internal request "
                f"{attempt.target!r} [{attempt.code}] ({attempt.reason}); the "
                f"verdict came from name resolution on this machine and no "
                f"traced fetch asked for it, so this is a delivery failure, "
                f"not an isolation violation — the call may be retried"
            )
        events = list(parsed.events)
        if asked_for:
            whose = "which a traced fetch asked for"
        else:
            whose = "which no traced fetch asked for"
            # Not in any fetch event's raw, so the evidence needs its own event.
            events.append(
                TraceEvent(
                    kind="other",
                    raw_type="egress/refused",
                    raw=_attempt_json(attempt),
                )
            )
        raise IsolationError(
            f"the egress filter refused {attempt.target!r} "
            f"[{attempt.code}] ({attempt.reason}), {whose}",
            events,
        )


def _check_targets(parsed: ParsedTranscript, *, resolve: Resolver | None) -> None:
    """D-4b, the after-the-fact layer that fires IN ADDITION to the filter: a
    non-public target is a violation even when nothing connected to it."""
    # Bound late, not as a default argument, so a caller (and a hermetic
    # test) can substitute the resolver for the whole call.
    cached = CachingResolver(resolve or default_resolver)
    for event in parsed.events:
        if event.kind == "search":
            for url in event.result_urls:
                # A search result nothing connected to: a host that simply fails
                # to resolve is not a violation (D-4b judges where a connection
                # actually goes), while anything non-public BY INSPECTION is.
                reason = classify_url(url, resolve=cached, unresolved_is_public=True)
                if reason is not None:
                    raise IsolationError(
                        f"non-public search result URL for query "
                        f"{event.query!r}: {reason}",
                        parsed.events,
                    )
            continue
        if event.kind != "fetch":
            continue
        for label, url in (("requested", event.url), ("final", event.final_url)):
            if url is None:
                continue
            reason = classify_url(url, resolve=cached)
            if reason is not None:
                raise IsolationError(
                    f"non-public {label} fetch target: {reason}", parsed.events
                )
