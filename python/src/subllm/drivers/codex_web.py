"""Web-agent mode for `codex exec` (D-2, D-3a, D-4, D-4b, D-4c, D-6).

Separate from `run_codex_exec`, whose behaviour is unchanged: this entry point
runs one browsing turn with every non-web capability disabled, an allowlisted
environment, a throwaway CODEX_HOME + HOME + working directory, and returns the
session rollout parsed into the D-4 trace. One attempt, no retry (D-4a).

Flags and shapes here are the measured set for codex-cli 0.154.0 /
gpt-5.6-luna; `--ephemeral` is deliberately NOT passed because it suppresses the
rollout, which IS the trace. The rollout lives inside the throwaway CODEX_HOME
and is deleted with it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from subllm.drivers.codex_exec import _QUOTA_PATTERNS, _isolated_codex_home
from subllm.drivers.codex_trace import ParsedRollout, parse_rollout
from subllm.errors import ClientError, IsolationError, QuotaError
from subllm.publictargets import CachingResolver, Resolver, classify_url, default_resolver
from subllm.webagent import TraceEvent

# D-6: the child gets these and nothing else. CODEX_HOME carries the auth copy;
# HOME points at an empty temp dir so the owner's ~/.agents/skills cannot enter
# the session (measured: with the real HOME they appear in the rollout).
ENV_ALLOWLIST = ("PATH", "HOME", "USER", "LANG", "TERM", "CODEX_HOME")

# D-2: every non-web capability switch, verified to exist on 0.154.0.
DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "unified_exec_tty",
    "browser_use", "browser_use_external", "browser_use_full_cdp_access",
    "computer_use", "apps", "multi_agent", "goals",
    "view_image", "image_generation", "sleep_tool",
    "hooks", "skill_search", "remote_plugin", "plugins",
    "in_app_browser", "tool_suggest",
)

# A disable that the client ignores. MEASURED on 0.154.0: `unified_exec` reports
# `true` whatever we pass (also with code_mode_host off), so the switch is not
# what closes the shell. What closes it is the offered tool set under the
# admitted version+model (D-3a): ALL_TOOLS held only apply_patch, multi_agent_*
# and web__run, and the G3 control could not run `ls`. Any OTHER feature we ask
# to disable must actually read back false.
INEFFECTIVE_DISABLES = {
    "unified_exec": "no effect on 0.154.0 (measured); shell absent from the "
                    "offered tool set under the admitted version+model",
}

# D-2: features that stay enabled with the overrides applied, each because it
# adds no callable tool to the model. Anything enabled and not listed here stops
# the call, so a client update that turns on a new capability blocks web-agent
# mode until it is reviewed.
ENABLED_ALLOWLIST = {
    "auth_elicitation": "auth prompt plumbing for the CLI, not a model tool",
    "code_mode_host": "the JS host the web tool itself runs in; removing it "
                      "removes web search (measured) — its runtime is closed "
                      "(no fetch/process/filesystem, measured in G3)",
    "collaboration_modes": "removed stage; TUI collaboration modes, no tool",
    "compaction_image_budget": "context-compaction budget, no tool",
    "content_item_kinds": "wire-format item kinds, no tool",
    "enable_request_compression": "HTTP request compression, no tool",
    "fast_mode": "latency profile for the turn, no tool",
    "guardian_approval": "approval routing for the CLI, no tool",
    "in_app_chat": "ChatGPT-app UI surface, not the exec session",
    "in_app_dictation": "ChatGPT-app UI surface, not the exec session",
    "in_app_local_automation": "ChatGPT-app UI surface, not the exec session",
    "in_app_updates": "ChatGPT-app updater, not the exec session",
    "item_ids": "wire-format ids on items, no tool",
    "mentions_v2": "@-mention parsing in the TUI, no tool",
    "personality": "system-prompt personality text, no tool",
    "plugin_sharing": "plugin sharing UI; plugins themselves are disabled",
    "remote_compaction_v2": "server-side context compaction, no tool",
    "resize_all_images": "image downscaling before upload, no tool",
    "shell_snapshot": "captures the launching shell's env for shell_tool, "
                      "which is disabled; no tool of its own",
    "skill_mcp_dependency_install": "installs MCP deps for skills; skill_search "
                                    "and plugins are disabled and the scratch "
                                    "home has no skills",
    "sqlite": "local history storage backend, no tool",
    "steer": "mid-turn steering input from the TUI, no tool",
    "terminal_resize_reflow": "TUI rendering, no tool",
    "tool_call_mcp_elicitation": "MCP elicitation prompts; no MCP server is "
                                 "configured in the scratch home",
    "tool_search_always_defer_mcp_tools": "MCP tool-search deferral; no MCP "
                                          "server is configured",
    "tui_app_server": "TUI app server, not used by exec",
    "unbounded_connection_retries": "transport retry policy, no tool",
    "unified_exec": INEFFECTIVE_DISABLES["unified_exec"],
    "unified_exec_zsh_fork": "zsh fork strategy for unified_exec, no tool of "
                             "its own",
    "workspace_dependencies": "workspace dependency metadata, no tool",
}

# D-3a: this client reports no tool inventory to the driver (verified: neither
# session_meta, turn_context, the rollout nor `codex debug prompt-input` names
# the offered tools), so admission is by recorded evidence. The tool set comes
# from the per-model catalog, so the record is keyed on (version, model): a
# different model on the same client version says nothing.
ADMITTED_VERSION_MODELS = frozenset({("0.154.0", "gpt-5.6-luna")})


@dataclass(frozen=True)
class CodexWebRun:
    answer: str
    trace: tuple[TraceEvent, ...]
    trace_complete: bool
    client_version: str
    duration_ms: int


def run_codex_web_task(
    prompt: str,
    *,
    model: str,
    tools: Literal["web", "none"] = "web",
    schema_path: str | None = None,
    timeout_s: int = 300,
    resolve: Resolver = default_resolver,
) -> CodexWebRun:
    """One isolated browsing (or no-tool) turn. Raises ClientError before the
    agent runs when the client version+model is not admitted or its feature
    inventory is unexpected, IsolationError (carrying the trace, never the
    answer) on any D-5 / D-4b violation, QuotaError on a subscription limit."""
    if tools not in ("web", "none"):
        raise ClientError(f"tools must be 'web' or 'none', got {tools!r}")

    with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False) as out_f:
        out_path = out_f.name
    scratch_home = _isolated_codex_home()
    work_dir = tempfile.mkdtemp(prefix="subllm-codex-cwd-")
    fake_home = tempfile.mkdtemp(prefix="subllm-codex-home-")
    env = _child_env(codex_home=scratch_home, home=fake_home)
    try:
        version = _client_version(env)
        if (version, model) not in ADMITTED_VERSION_MODELS:
            raise ClientError(
                f"codex web-agent mode is not admitted for client {version!r} "
                f"with model {model!r}: the client reports no tool inventory, so "
                f"a version+model pair runs only after it passes the live G3 "
                f"negative control (D-3a). Re-run that control and add the pair."
            )
        _check_feature_inventory(env)

        argv = _build_argv(
            prompt,
            model=model,
            tools=tools,
            out_path=out_path,
            work_dir=work_dir,
            schema_path=schema_path,
        )
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                env=env,
                cwd=work_dir,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except FileNotFoundError as e:
            raise ClientError("codex binary not found on PATH") from e
        except subprocess.TimeoutExpired as e:
            raise ClientError(f"codex exec timed out after {timeout_s}s") from e
        duration_ms = int((time.monotonic() - started) * 1000)

        if proc.returncode != 0:
            blob = f"{proc.stderr or ''}\n{proc.stdout or ''}".strip()
            if _QUOTA_PATTERNS.search(blob):
                raise QuotaError(f"codex subscription limit: {blob[:200]}")
            raise ClientError(
                f"codex exec failed (exit {proc.returncode}): {blob[:200]}"
            )

        rollouts = _read_rollouts(scratch_home)
        answer = Path(out_path).read_text()
    finally:
        for path in (scratch_home, work_dir, fake_home):
            shutil.rmtree(path, ignore_errors=True)
        try:
            os.unlink(out_path)
        except OSError:
            pass

    if not rollouts:
        raise ClientError(
            "codex wrote no session rollout, so there is no trace to check"
        )
    if len(rollouts) > 1:
        # A spawned sub-agent writes its own rollout into the same home
        # (measured). More than one session means the turn was not alone.
        raise IsolationError(
            f"{len(rollouts)} session rollouts in the scratch home — a "
            f"sub-agent session was spawned",
            _flatten(rollouts),
        )

    parsed = parse_rollout(rollouts[0], tools=tools)
    if parsed.violations:
        raise IsolationError(
            "codex web-agent isolation violation: " + "; ".join(parsed.violations),
            parsed.events,
        )
    _check_fetch_targets(parsed, resolve=resolve)
    if parsed.client_version and parsed.client_version != version:
        raise ClientError(
            f"rollout reports client version {parsed.client_version!r}, but "
            f"{version!r} was admitted"
        )

    return CodexWebRun(
        answer=answer,
        trace=tuple(parsed.events),
        trace_complete=parsed.trace_complete,
        client_version=parsed.client_version or version,
        duration_ms=duration_ms,
    )


def _build_argv(
    prompt: str,
    *,
    model: str,
    tools: str,
    out_path: str,
    work_dir: str,
    schema_path: str | None,
) -> list[str]:
    argv = [
        "codex", "exec",
        "-s", "read-only",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "-c", "project_doc_max_bytes=0",
        # Measured: merely omitting web_search still OFFERS a working web tool,
        # so tools='none' must disable it explicitly.
        "-c", 'web_search="live"' if tools == "web" else 'web_search="disabled"',
    ]
    for feature in DISABLED_FEATURES:
        argv += ["--disable", feature]
    argv += ["-C", work_dir, "-m", model, "--json", "-o", out_path]
    if schema_path:
        argv += ["--output-schema", schema_path]
    argv += ["--", prompt]
    return argv


def _child_env(*, codex_home: str, home: str) -> dict[str, str]:
    """D-6: an explicit allowlist, built from scratch. Nothing else in the
    caller's environment — database URLs, API keys — reaches the child."""
    env: dict[str, str] = {}
    for name in ENV_ALLOWLIST:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    env["HOME"] = home
    env["CODEX_HOME"] = codex_home
    env.setdefault("PATH", os.defpath)
    return env


def _client_version(env: dict[str, str]) -> str:
    try:
        proc = subprocess.run(
            ["codex", "--version"],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as e:
        raise ClientError("codex binary not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise ClientError("codex --version timed out") from e
    if proc.returncode != 0:
        raise ClientError(f"codex --version failed: {(proc.stderr or '')[:200]}")
    text = (proc.stdout or "").strip()
    return text.split()[-1] if text else ""


def _check_feature_inventory(env: dict[str, str]) -> None:
    """D-2: read the installed client's inventory with the web-agent overrides
    applied. A switch we intend to use that no longer exists, a disable that did
    not take, or an enabled feature that is not on the reviewed allowlist stops
    the call."""
    argv = ["codex", "features", "list"]
    for feature in DISABLED_FEATURES:
        argv += ["--disable", feature]
    try:
        proc = subprocess.run(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as e:
        raise ClientError("codex binary not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise ClientError("codex features list timed out") from e
    if proc.returncode != 0:
        raise ClientError(
            f"codex features list failed: {(proc.stderr or '')[:200]}"
        )

    states: dict[str, bool] = {}
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[-1] not in ("true", "false"):
            continue
        states[parts[0]] = parts[-1] == "true"
    if not states:
        raise ClientError("codex features list produced no readable inventory")

    missing = [f for f in DISABLED_FEATURES if f not in states]
    if missing:
        raise ClientError(
            f"codex feature switches missing from this client: {missing} — the "
            f"capability they close may have been renamed"
        )
    ignored = [
        f for f in DISABLED_FEATURES
        if states[f] and f not in INEFFECTIVE_DISABLES
    ]
    if ignored:
        raise ClientError(
            f"codex ignored these disables: {ignored} — a capability we "
            f"intend to close is still enabled"
        )
    unexpected = sorted(
        name for name, on in states.items() if on and name not in ENABLED_ALLOWLIST
    )
    if unexpected:
        raise ClientError(
            f"codex features enabled but not on the web-agent allowlist: "
            f"{unexpected} — review them before running web-agent mode"
        )


def _read_rollouts(scratch_home: str) -> list[list[dict[str, Any]]]:
    """Every session rollout written inside the throwaway home, read before the
    home is deleted."""
    rollouts: list[list[dict[str, Any]]] = []
    for path in sorted(Path(scratch_home).rglob("rollout-*.jsonl")):
        records: list[dict[str, Any]] = []
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                records.append({"type": "<unparsable>", "payload": {"line": line}})
                continue
            records.append(record if isinstance(record, dict) else {"type": "<unparsable>"})
        rollouts.append(records)
    return rollouts


def _flatten(rollouts: list[list[dict[str, Any]]]) -> list[TraceEvent]:
    events: list[TraceEvent] = []
    for records in rollouts:
        events.extend(parse_rollout(records).events)
    return events


def _check_fetch_targets(parsed: ParsedRollout, *, resolve: Resolver) -> None:
    """D-4b: every traced fetch's requested AND final target must be a public
    http/https address. For this client the check is after the fact — the fetch
    runs on the vendor's servers, so subllm cannot block the connection, it can
    only refuse to return an answer that involved a non-public target."""
    cached = CachingResolver(resolve)
    for event in parsed.events:
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
