"""Hermetic tests for claude web-agent mode: no network, no real `claude`.

The trace fixtures are live captures from claude 2.1.276 /
claude-haiku-4-5-20251001, sanitized (see test_fixtures_are_sanitized). The fake
`tmux` on PATH copies a chosen transcript fixture into the throwaway
CLAUDE_CONFIG_DIR exactly where the real client writes it, and dumps the
environment the tmux SERVER was started with — which is the D-6 evidence.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from subllm.claude import ClaudeLLM
from subllm.drivers import claude_web
from subllm.drivers.claude_trace import parse_transcript
from subllm.drivers.egress import SANDBOX_EXEC, Attempt
from subllm.errors import ClientError, IsolationError
from subllm.webagent import TraceEvent, derive_retrieved_urls

FIXTURES = Path(__file__).parent / "fixtures"
G1 = FIXTURES / "claude-2.1.276-g1-fetch.jsonl"
REDIRECT = FIXTURES / "claude-2.1.276-redirect.jsonl"
SEARCH = FIXTURES / "claude-2.1.276-search.jsonl"
G3 = FIXTURES / "claude-2.1.276-g3-negative-control.jsonl"
NONE_TOOLS = FIXTURES / "claude-2.1.276-none-tools.jsonl"
# re-recorded live on 2.1.277 when the reason CODE was added: a derived
# field must come from a real run, not be hand-edited into a capture
G3_ATTEMPTS = FIXTURES / "claude-2.1.277-g3-egress-attempts.json"

MODEL = "claude-haiku-4-5-20251001"

needs_sandbox = pytest.mark.skipif(
    not os.path.isfile(SANDBOX_EXEC),
    reason="sandbox-exec is the D-4b confinement; not available here",
)


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_transcript(path: Path, recs: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    return path


def only_public(host: str) -> list[str]:
    """Injected resolver: every hostname is public. Keeps tests off DNS."""
    return ["93.184.216.34"]


def loopback_for(name: str):
    def resolve(host: str) -> list[str]:
        return ["127.0.0.1"] if host == name else ["93.184.216.34"]

    return resolve


# --- the stub client ---------------------------------------------------------

_FAKE_TMUX = r'''#!/usr/bin/env python3
"""A fake tmux server. Parses the real driver's argv, plants the transcript
fixture where the real client would write it, and logs its environment."""
import json, os, re, shutil, sys
from pathlib import Path

argv = sys.argv[1:]
probe = Path("@PROBE@")
cfg = json.loads((probe / "config.json").read_text())
# strip the server flags the driver always passes
assert argv[0] == "-L", argv
socket_name, argv = argv[1], argv[2:]
assert argv[0] == "-f" and argv[1] == "/dev/null", argv
argv = argv[2:]
command = argv[0]

if command == "new-session":
    (probe / "env.json").write_text(json.dumps(dict(os.environ)))
    (probe / "socket.txt").write_text(socket_name)
    work_dir = argv[argv.index("-c") + 1]
    cmd = argv[-1]
    (probe / "argv.txt").write_text(cmd)
    session_id = re.search(r"--session-id'?\s+'?([0-9a-f-]{36})", cmd).group(1)
    config_dir = Path(os.environ["CLAUDE_CONFIG_DIR"])
    (probe / "credentials.json").write_text(
        (config_dir / ".credentials.json").read_text()
    )
    (probe / "claude.json").write_text((config_dir / ".claude.json").read_text())
    (probe / "config_dir.txt").write_text(str(config_dir))
    encoded = re.sub(r"[^A-Za-z0-9-]", "-", str(Path(work_dir).resolve()))
    project = config_dir / "projects" / encoded
    project.mkdir(parents=True, exist_ok=True)
    fixture = cfg["fixture"]
    if fixture:
        shutil.copy(fixture, project / f"{session_id}.jsonl")
    if cfg["extra"]:
        shutil.copy(cfg["extra"], project / "another-session.jsonl")
    if cfg["subagents"]:
        (project / session_id / "subagents").mkdir(parents=True, exist_ok=True)
    sys.exit(0)

if command == "send-keys":
    if "-l" in argv:
        (probe / "sendkeys.txt").write_text(argv[argv.index("-l") + 1])
    sys.exit(0)

if command == "capture-pane":
    sys.stdout.write(cfg["pane"])
    sys.exit(0)

if command == "show-environment":
    # The REAL server reports the environment it was started with, not this
    # process's: these calls carry no env= and would otherwise echo pytest's.
    reported = json.loads((probe / "env.json").read_text())
    reported.update(cfg["leak_server_env"])
    sys.stdout.write("\n".join(f"{k}={v}" for k, v in sorted(reported.items())))
    sys.exit(0)

sys.exit(0)
'''


@pytest.fixture
def claude_stub(fake_bin, tmp_path, monkeypatch):
    """Install fake `tmux`, `claude` and `security` binaries. Returns a callable
    that (re)configures them, plus the probe directory the stub writes to."""
    probe = tmp_path / "probe"
    probe.mkdir()
    # The turn must not wait 1.5 s per settle poll in a hermetic test.
    monkeypatch.setattr(claude_web, "_SETTLE_QUIET_S", 0.01)
    monkeypatch.setattr(claude_web, "_SETTLE_LIMIT_S", 0.2)

    def configure(
        *,
        fixture: Path | None = G1,
        version: str = "2.1.276",
        credential: dict | None = None,
        extra_transcript: Path | None = None,
        subagents: bool = False,
        pane: str | None = None,
        leak_server_env: dict | None = None,
    ) -> Path:
        (probe / "config.json").write_text(json.dumps({
            "fixture": str(fixture) if fixture else "",
            "extra": str(extra_transcript) if extra_transcript else "",
            "subagents": bool(subagents),
            "pane": pane if pane is not None else "\u276f ",
            "leak_server_env": leak_server_env or {},
        }))
        blob = credential if credential is not None else {
            "claudeAiOauth": {"accessToken": "sk-ant-oat01-TESTTOKEN"},
            # the real keychain blob also carries MCP server secrets, which an
            # auth-only home must never receive
            "mcpOAuth": {"supabase": {"clientSecret": "MCP-SECRET"}},
        }
        fake_bin("tmux", _FAKE_TMUX.replace("@PROBE@", str(probe)))
        fake_bin("claude", f'#!/bin/bash\necho "{version} (Claude Code)"\n')
        fake_bin(
            "security",
            "#!/bin/bash\ncat <<'EOF'\n" + json.dumps(blob) + "\nEOF\n",
        )
        return probe

    return configure


def run_task(prompt: str = "go", *, tools: str = "web", model: str = MODEL,
             resolve=only_public):
    return claude_web.run_claude_web_task(
        prompt, model=model, tools=tools, resolve=resolve
    )


# --- trace parsing (D-4, G2) -------------------------------------------------


def test_g1_fixture_parses_to_the_spec_shape():
    parsed = parse_transcript(records(G1))
    assert parsed.violations == []
    assert parsed.trace_complete, parsed.incomplete_reasons
    assert parsed.client_version == "2.1.276"
    assert parsed.model == MODEL
    assert [(e.kind, e.url, e.final_url, e.ok) for e in parsed.events] == [
        ("fetch", "https://example.com", "https://example.com", True)
    ]
    assert derive_retrieved_urls(parsed.events) == ("https://example.com",)
    # D-4: the client's own records travel with the classification.
    assert parsed.events[0].raw["call"]["type"] == "assistant"
    assert parsed.events[0].raw["result"]["toolUseResult"]["code"] == 200


def test_a_failed_fetch_is_not_retrieved_and_has_no_final_url():
    # The committed redirect capture IS the evidence for the final_url rule:
    # this client does NOT auto-follow a cross-host redirect — it reports the
    # 302 and asks the model to fetch the new URL itself — so a 2xx result's
    # URL is the address the content came from, and a non-2xx is no retrieval.
    parsed = parse_transcript(records(REDIRECT))
    assert parsed.violations == []
    event = parsed.events[0]
    assert event.url == "https://httpbin.org/redirect-to?url=https://example.com"
    assert event.raw["result"]["toolUseResult"]["code"] == 302
    assert "REDIRECT DETECTED" in event.raw["result"]["toolUseResult"]["result"]
    assert event.ok is False
    assert event.final_url is None
    assert derive_retrieved_urls(parsed.events) == ()


def test_a_non_2xx_status_never_yields_a_final_url():
    recs = records(G1)
    for rec in recs:
        if isinstance(rec.get("toolUseResult"), dict):
            rec["toolUseResult"]["code"] = 404
    event = parse_transcript(recs).events[0]
    assert (event.ok, event.final_url) == (False, None)
    assert derive_retrieved_urls([event]) == ()


def test_search_results_are_exposed_and_are_not_retrieved():
    parsed = parse_transcript(records(SEARCH))
    assert parsed.violations == []
    assert parsed.trace_complete, parsed.incomplete_reasons
    event = parsed.events[0]
    assert event.kind == "search"
    assert event.query == "IETF RFC 9110 HTTP semantics"
    assert "https://www.rfc-editor.org/info/rfc9110/" in event.result_urls
    assert derive_retrieved_urls(parsed.events) == ()


def test_a_refused_fetch_reports_a_failure_not_unknown_evidence():
    # The egress filter's refusal and the client's own `file://` refusal both
    # arrive as a plain-string toolUseResult (measured in the G3 capture).
    parsed = parse_transcript(records(G3))
    assert parsed.violations == []
    assert parsed.trace_complete, parsed.incomplete_reasons
    assert [(e.url, e.ok, e.final_url) for e in parsed.events] == [
        ("http://127.0.0.1:49613/loopback", False, None),
        ("http://127.0.0.1.nip.io:49613/nipio", False, None),
        ("file:///private/tmp/subllm-canary/subllm-canary.txt", False, None),
    ]


def test_a_tool_result_string_is_carried_in_the_raw_record():
    parsed = parse_transcript(records(G3))
    blob = json.dumps([e.to_json() for e in parsed.events])
    assert "proxy refused the connection" in blob


# --- isolation violations (D-5, D-3a, D-4c) ----------------------------------


def test_a_non_web_tool_use_is_a_violation():
    recs = records(G1)
    for rec in recs:
        for block in (rec.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                block["name"] = "Bash"
                block["input"] = {"command": "cat /etc/passwd"}
    parsed = parse_transcript(recs)
    assert any("'Bash' is not a web tool" in v for v in parsed.violations)
    # the violating call's RESULT is carried: that is where a leak would show
    assert any(e.raw_type == "tool_result:Bash" for e in parsed.events)
    assert not any(e.kind == "fetch" for e in parsed.events)


def test_any_tool_use_under_tools_none_is_a_violation():
    parsed = parse_transcript(records(G1), tools="none")
    assert any("tools='none'" in v for v in parsed.violations)
    assert not any(e.kind == "fetch" for e in parsed.events)


def test_the_committed_tools_none_capture_makes_no_tool_call():
    parsed = parse_transcript(records(NONE_TOOLS), tools="none")
    assert parsed.violations == []
    assert parsed.trace_complete, parsed.incomplete_reasons
    assert parsed.events == []
    assert parsed.answer.strip() == "5"


def test_an_unrecognised_record_that_may_be_a_tool_is_a_violation():
    recs = records(G1)
    recs.insert(3, {"type": "sidechain-invocation", "payload": {"tool": "Task"}})
    parsed = parse_transcript(recs)
    assert any("may be a tool" in v for v in parsed.violations)


def test_an_unrecognised_assistant_content_block_is_a_violation():
    recs = records(G1)
    for rec in recs:
        content = (rec.get("message") or {}).get("content")
        if rec.get("type") == "assistant" and isinstance(content, list):
            content.append({"type": "server_tool_use", "name": "code_execution"})
            break
    parsed = parse_transcript(recs)
    assert any("server_tool_use" in v for v in parsed.violations)


def test_an_inventory_naming_a_non_web_tool_is_a_violation():
    recs = records(G1)
    for rec in recs:
        attachment = rec.get("attachment") or {}
        if isinstance(attachment.get("tools"), list):
            attachment["tools"].append({"name": "Bash", "description": "run"})
    parsed = parse_transcript(recs)
    assert any("offered the model tool(s) ['Bash']" in v for v in parsed.violations)


def test_the_inventory_must_be_empty_under_tools_none():
    # The committed tools='none' capture reports an EMPTY inventory; a web tool
    # in it would mean the client offered one anyway.
    recs = records(NONE_TOOLS)
    for rec in recs:
        attachment = rec.get("attachment") or {}
        if isinstance(attachment.get("tools"), list):
            attachment["tools"].append({"name": "WebFetch"})
    parsed = parse_transcript(recs, tools="none")
    assert any("WebFetch" in v for v in parsed.violations)


def test_a_deferred_tools_delta_adding_a_non_web_tool_is_a_violation():
    recs = records(G1)
    recs.insert(
        5,
        {
            "type": "attachment",
            "attachment": {"type": "deferred_tools_delta",
                           "addedNames": ["WebFetch", "Read"]},
        },
    )
    parsed = parse_transcript(recs)
    assert any("deferred_tools_delta offered non-web tool(s) ['Read']" in v
               for v in parsed.violations)


@pytest.mark.parametrize(
    "atype", ["skill_listing", "mcp_instructions_delta", "agent_listing_delta"]
)
def test_a_non_empty_settings_attachment_is_a_violation(atype):
    recs = records(G1)
    recs.insert(
        5,
        {"type": "attachment",
         "attachment": {"type": atype, "content": "you may use skill X"}},
    )
    parsed = parse_transcript(recs)
    assert any(atype in v for v in parsed.violations)


def test_an_empty_settings_attachment_is_not_a_violation():
    recs = records(G1)
    recs.insert(5, {"type": "attachment",
                    "attachment": {"type": "skill_listing", "content": ""}})
    assert parse_transcript(recs).violations == []


def test_a_file_history_snapshot_with_backups_is_a_violation():
    recs = records(G1)
    for rec in recs:
        if rec.get("type") == "file-history-snapshot":
            rec["snapshot"]["trackedFileBackups"] = {"/etc/hosts": "backup-1"}
    parsed = parse_transcript(recs)
    assert any("file was" in v for v in parsed.violations)


# --- incomplete evidence (D-4) ----------------------------------------------


def test_an_unpaired_tool_call_marks_the_trace_incomplete():
    recs = [r for r in records(G1) if "toolUseResult" not in r]
    parsed = parse_transcript(recs)
    assert parsed.violations == []
    assert not parsed.trace_complete
    assert any("no matching result" in r for r in parsed.incomplete_reasons)


def test_a_truncated_transcript_marks_the_trace_incomplete():
    recs = records(G1)
    recs.append({"type": "<unparsable>", "line": "{not json"})
    parsed = parse_transcript(recs)
    assert parsed.violations == []
    assert not parsed.trace_complete


def test_zero_web_events_on_a_web_call_marks_the_trace_incomplete():
    parsed = parse_transcript(records(NONE_TOOLS), tools="web")
    assert parsed.violations == []
    assert not parsed.trace_complete
    assert "no web events on a web-tool call" in parsed.incomplete_reasons


def test_a_missing_tool_inventory_marks_the_trace_incomplete():
    recs = [
        r for r in records(G1)
        if not isinstance((r.get("attachment") or {}).get("tools"), list)
    ]
    parsed = parse_transcript(recs)
    assert parsed.violations == []
    assert not parsed.trace_complete
    assert any("tool inventory" in r for r in parsed.incomplete_reasons)


@needs_sandbox
def test_an_incomplete_trace_yields_an_empty_retrieved_list(
    claude_stub, tmp_path, monkeypatch
):
    claude_stub(fixture=write_transcript(
        tmp_path / "t.jsonl", [r for r in records(G1) if "toolUseResult" not in r]
    ))
    monkeypatch.setattr(claude_web, "default_resolver", only_public)
    result = ClaudeLLM(model=MODEL).run_web_task("go")
    assert not result.trace_complete
    assert result.retrieved_urls == ()


# --- the driver (D-3, D-3a, D-4b, D-6) --------------------------------------


@needs_sandbox
def test_the_client_method_returns_the_4_1_shape(claude_stub, monkeypatch):
    claude_stub()
    monkeypatch.setattr(claude_web, "default_resolver", only_public)
    result = ClaudeLLM(model=MODEL).run_web_task("go")
    assert result.client == "claude"
    assert result.model == MODEL
    assert result.tools == "web"
    assert result.client_version == "2.1.276"
    assert result.retrieved_urls == ("https://example.com",)
    assert result.trace_complete and result.fetch_events_exposed
    assert result.duration_ms >= 0
    assert "Example Domain" in result.answer
    assert [e.kind for e in result.trace] == ["fetch"]
    # §4.2 serialization is the consumer's contract
    document = result.to_json()
    assert document["trace"][0]["final_url"] == "https://example.com"
    assert document["retrieved_urls"] == ["https://example.com"]


@needs_sandbox
def test_the_client_method_requires_a_model(claude_stub):
    claude_stub()
    with pytest.raises(ClientError):
        ClaudeLLM().run_web_task("go")


@needs_sandbox
def test_an_unadmitted_client_version_is_a_client_error(claude_stub):
    claude_stub(version="2.2.0")
    with pytest.raises(ClientError) as excinfo:
        run_task()
    assert "not admitted" in str(excinfo.value)
    assert "G3" in str(excinfo.value)


@needs_sandbox
def test_a_version_the_transcript_disagrees_with_is_a_client_error(
    claude_stub, tmp_path
):
    recs = records(G1)
    for rec in recs:
        if rec.get("version"):
            rec["version"] = "2.1.999"
    claude_stub(fixture=write_transcript(tmp_path / "t.jsonl", recs))
    with pytest.raises(ClientError) as excinfo:
        run_task()
    assert "2.1.999" in str(excinfo.value)


@needs_sandbox
def test_a_non_web_tool_call_raises_an_isolation_error(claude_stub, tmp_path):
    recs = records(G1)
    for rec in recs:
        for block in (rec.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                block["name"] = "Bash"
    claude_stub(fixture=write_transcript(tmp_path / "t.jsonl", recs))
    with pytest.raises(IsolationError) as excinfo:
        run_task()
    assert "not a web tool" in str(excinfo.value)
    assert excinfo.value.trace  # the trace travels with the error (D-5)


@needs_sandbox
def test_a_non_public_fetch_target_raises_an_isolation_error(claude_stub):
    # The committed G3 capture: the client asked for loopback (and a
    # public-looking name that resolves to it). The egress filter refused the
    # connection BEFORE it happened; this after-the-fact layer fires too, so
    # both layers are load-bearing (D-4b).
    claude_stub(fixture=G3)
    with pytest.raises(IsolationError) as excinfo:
        run_task(resolve=loopback_for("127.0.0.1.nip.io"))
    assert "non-public requested fetch target" in str(excinfo.value)


@needs_sandbox
def test_a_search_result_that_is_not_public_raises_an_isolation_error(
    claude_stub, tmp_path
):
    recs = records(SEARCH)
    for rec in recs:
        result = rec.get("toolUseResult")
        if isinstance(result, dict) and result.get("results"):
            result["results"][0]["content"][0]["url"] = "http://10.0.0.5/intranet"
    claude_stub(fixture=write_transcript(tmp_path / "t.jsonl", recs))
    with pytest.raises(IsolationError) as excinfo:
        run_task()
    assert "non-public search result URL" in str(excinfo.value)


@needs_sandbox
def test_a_search_result_that_does_not_resolve_is_not_a_violation(
    claude_stub, tmp_path
):
    # Nothing connected to a search result, and D-4b judges where a connection
    # actually goes: an NXDOMAIN result must not withhold a legitimate answer.
    def never_resolves(host: str) -> list[str]:
        raise OSError("nxdomain")

    claude_stub(fixture=SEARCH)
    run = run_task(resolve=never_resolves)
    assert run.trace_complete


@needs_sandbox
def test_a_fetch_target_that_does_not_resolve_still_fails_closed(claude_stub):
    def never_resolves(host: str) -> list[str]:
        raise OSError("nxdomain")

    claude_stub(fixture=G1)
    with pytest.raises(IsolationError) as excinfo:
        run_task(resolve=never_resolves)
    assert "could not be resolved" in str(excinfo.value)


@needs_sandbox
def test_a_subagent_transcript_directory_raises_an_isolation_error(claude_stub):
    claude_stub(subagents=True)
    with pytest.raises(IsolationError) as excinfo:
        run_task()
    assert "sub-agent transcripts" in str(excinfo.value)


@needs_sandbox
def test_a_second_session_transcript_raises_an_isolation_error(claude_stub):
    claude_stub(extra_transcript=G1)
    with pytest.raises(IsolationError) as excinfo:
        run_task()
    assert "sub-agent transcripts" in str(excinfo.value)


@needs_sandbox
def test_a_dialog_instead_of_the_prompt_fails_loudly(claude_stub):
    # The workspace-trust dialog's default option is "No, exit" and its own
    # selection cursor is a banner marker, so it is never answered blindly.
    claude_stub(pane="Quick safety check: Is this a project you created "
                     "or one you trust?\n  ❯ 1. No, exit")
    with pytest.raises(ClientError) as excinfo:
        run_task()
    assert "Quick safety check" in str(excinfo.value)


# --- D-6 environment allowlist ----------------------------------------------


@needs_sandbox
def test_the_tmux_server_environment_is_exactly_the_allowlist(
    claude_stub, monkeypatch
):
    monkeypatch.setenv("SUBLLM_SENTINEL", "SENTINEL-must-not-leak")
    monkeypatch.setenv("DATABASE_URL", "postgres://secret@host/db")
    probe = claude_stub()
    run_task()
    env = json.loads((probe / "env.json").read_text())
    # __CF_USER_TEXT_ENCODING is injected by macOS itself into every spawned
    # process; it is not read from the caller's environment.
    assert set(env) - {"__CF_USER_TEXT_ENCODING"} == (
        set(claude_web.ENV_ALLOWLIST) | set(claude_web.PROXY_VARS)
    )
    assert "SUBLLM_SENTINEL" not in env
    assert "SENTINEL-must-not-leak" not in json.dumps(env)
    assert "DATABASE_URL" not in env
    # the proxy variables all point at the egress filter, and NO_PROXY is never
    # forwarded (it would punch a hole in D-4b)
    assert len({env[name] for name in claude_web.PROXY_VARS}) == 1
    assert env["HTTP_PROXY"].startswith("http://127.0.0.1:")
    assert not any(name.lower() == "no_proxy" for name in env)
    assert env["CLAUDE_CONFIG_DIR"] != os.environ.get("HOME")


@needs_sandbox
def test_the_server_reports_only_the_allowlist_back(claude_stub, monkeypatch):
    # D-6 is checked on the process that ACTUALLY ran the agent: the dedicated
    # server's own global environment is read back before it is killed.
    monkeypatch.setenv("SUBLLM_SENTINEL", "SENTINEL-must-not-leak")
    claude_stub()
    turn = claude_web.run_turn("go", model=MODEL, resolve=only_public)
    assert set(turn.server_env) - {"__CF_USER_TEXT_ENCODING"} == (
        set(claude_web.ENV_ALLOWLIST) | set(claude_web.PROXY_VARS)
    )
    assert "SENTINEL-must-not-leak" not in json.dumps(turn.server_env)


@needs_sandbox
def test_a_variable_the_server_gained_stops_the_call(claude_stub, monkeypatch):
    monkeypatch.setenv("SUBLLM_SENTINEL", "SENTINEL-must-not-leak")
    claude_stub(leak_server_env={"SUBLLM_SENTINEL": "SENTINEL-must-not-leak"})
    with pytest.raises(ClientError) as excinfo:
        run_task()
    assert "SUBLLM_SENTINEL" in str(excinfo.value)
    assert "D-6" in str(excinfo.value)


@needs_sandbox
def test_the_child_runs_in_an_empty_home_and_working_directory(claude_stub):
    probe = claude_stub()
    run_task()
    env = json.loads((probe / "env.json").read_text())
    assert env["HOME"] != os.path.expanduser("~")
    assert not os.path.exists(env["HOME"])  # deleted with the call


@needs_sandbox
def test_the_client_is_launched_confined_with_web_only_tools(claude_stub):
    probe = claude_stub()
    run_task()
    command = (probe / "argv.txt").read_text()
    assert command.startswith(f"'{SANDBOX_EXEC}' '-f' ")
    assert "'--tools' 'WebSearch,WebFetch'" in command
    assert "'--permission-mode' 'bypassPermissions'" in command
    assert "'--strict-mcp-config'" in command
    assert "'--disable-slash-commands'" in command
    assert "'--safe-mode'" in command
    assert "--restricted" not in command and "--bare" not in command


@needs_sandbox
def test_tools_none_offers_no_tool_at_all(claude_stub):
    probe = claude_stub(fixture=NONE_TOOLS)
    run = run_task(tools="none")
    assert run.trace == ()
    assert "'--tools' ''" in (probe / "argv.txt").read_text()


# --- the dedicated configuration home (D-3) ---------------------------------


@needs_sandbox
def test_the_config_home_carries_only_the_oauth_key_and_is_deleted(claude_stub):
    probe = claude_stub()
    run_task()
    seeded = json.loads((probe / "credentials.json").read_text())
    assert list(seeded) == ["claudeAiOauth"]
    assert "mcpOAuth" not in json.dumps(seeded)
    assert "MCP-SECRET" not in json.dumps(seeded)
    config_dir = (probe / "config_dir.txt").read_text()
    assert not os.path.exists(config_dir), "the throwaway config home survived"


@needs_sandbox
def test_the_config_home_skips_onboarding_for_the_resolved_work_dir(claude_stub):
    probe = claude_stub()
    run_task()
    seeded = json.loads((probe / "claude.json").read_text())
    assert seeded["hasCompletedOnboarding"] is True
    assert seeded["bypassPermissionsModeAccepted"] is True
    project_keys = list(seeded["projects"])
    assert len(project_keys) == 1
    assert project_keys[0] == str(Path(project_keys[0]).resolve())
    assert seeded["projects"][project_keys[0]]["hasTrustDialogAccepted"] is True
    assert seeded["projects"][project_keys[0]]["mcpServers"] == {}


@needs_sandbox
def test_a_credential_without_the_oauth_key_fails_before_the_client_runs(
    claude_stub,
):
    claude_stub(credential={"mcpOAuth": {"x": "y"}})
    with pytest.raises(ClientError) as excinfo:
        run_task()
    assert "claudeAiOauth" in str(excinfo.value)


# --- the egress filter's evidence (D-4, D-4b) -------------------------------


def _attempt(**kwargs) -> Attempt:
    base = dict(at=1.0, method="CONNECT", target="example.com:443",
                host="example.com", port=443, address="93.184.216.34",
                allowed=True, reason=None, connected=True, code=None)
    base.update(kwargs)
    return Attempt(**base)


def test_filter_attempts_travel_in_the_fetch_events_raw_record():
    parsed = parse_transcript(records(G1))
    claude_web._attach_attempts(parsed, (_attempt(),))
    attached = parsed.events[0].raw["egress_attempts"]
    assert attached[0]["host"] == "example.com"
    assert attached[0]["allowed"] is True and attached[0]["connected"] is True


def test_the_committed_g3_attempts_show_the_refusals_against_loopback():
    attempts = json.loads(G3_ATTEMPTS.read_text())
    refusals = [a for a in attempts if not a["allowed"]]
    assert {a["host"] for a in refusals} == {"127.0.0.1", "127.0.0.1.nip.io"}
    assert all("not a public address" in a["reason"]
               or "resolves to non-public address 127.0.0.1" in a["reason"]
               for a in refusals)
    # the machine-readable codes the consumer judges on, both forms present
    assert {a["code"] for a in refusals} == {"non_public", "resolved_non_public"}
    assert all(a["code"] is None for a in attempts if a["allowed"])
    # a refusal never connected, and an allowed attempt is only evidence of a
    # reached destination when it also connected
    assert all(a["connected"] is False for a in refusals)
    assert any(a["allowed"] and a["connected"] for a in attempts)
    parsed = parse_transcript(records(G3))
    claude_web._attach_attempts(parsed, tuple(_attempt(**{
        k: v for k, v in a.items()}) for a in attempts))
    blob = json.dumps([e.to_json() for e in parsed.events])
    assert "resolves to non-public address 127.0.0.1" in blob


def test_a_refusal_no_traced_fetch_asked_for_is_an_isolation_error():
    parsed = parse_transcript(records(G1))
    refusal = _attempt(target="10.0.0.5:443", host="10.0.0.5", port=443,
                       address="10.0.0.5", allowed=False, connected=False,
                       reason="10.0.0.5 is not a public address",
                       code="non_public")
    with pytest.raises(IsolationError) as excinfo:
        claude_web._check_refusals(parsed, (refusal,))
    assert "10.0.0.5:443" in str(excinfo.value)
    assert any(e.raw_type == "egress/refused" for e in excinfo.value.trace)


def test_a_recorded_refusal_wins_over_a_later_re_resolution():
    # The filter refused this host at connection time because the name resolved
    # to loopback THEN. By judgement time it resolves public again (DNS
    # rebinding, or a TTL rolling to a CDN), so `_check_targets` would pass —
    # the recorded verdict is the authoritative D-4b evidence and the answer is
    # never returned alongside it (D-5).
    recs = records(G1)
    for rec in recs:
        for block in (rec.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                block["input"]["url"] = "https://rebinding.test/x"
        if isinstance(rec.get("toolUseResult"), dict):
            rec["toolUseResult"]["url"] = "https://rebinding.test/x"
    refusal = _attempt(target="rebinding.test:443", host="rebinding.test",
                       port=443, address=None, allowed=False, connected=False,
                       reason="host 'rebinding.test' resolves to non-public "
                              "address 127.0.0.1: 'https://rebinding.test/x'",
                       code="resolved_non_public")
    with pytest.raises(IsolationError) as excinfo:
        claude_web.judge_turn(
            _turn(recs, [refusal]), tools="web", model=MODEL,
            # resolves PUBLIC now — only the recorded refusal can catch this
            resolve=only_public,
        )
    assert "which a traced fetch asked for" in str(excinfo.value)
    assert "resolves to non-public address 127.0.0.1" in str(excinfo.value)


def test_a_port_rule_refusal_is_not_an_isolation_error():
    # `judge_target` checks publicness BEFORE the port rule, so a port-rule
    # refusal means the target WAS a public http/https host: a policy refusal,
    # not evidence that a connection would have reached a non-public address.
    parsed = parse_transcript(records(G1))
    refusal = _attempt(target="example.com:8443", host="example.com", port=8443,
                       address=None, allowed=False, connected=False,
                       reason="port 8443 is not one of (80, 443): "
                              "'https://example.com:8443/'", code="port")
    claude_web._check_refusals(parsed, (refusal,))  # no raise
    claude_web.judge_turn(
        _turn(records(G1), [refusal]), tools="web", model=MODEL,
        resolve=only_public,
    )


def test_the_filter_reports_a_machine_readable_code_for_every_refusal():
    # The consumer must never classify the prose. This also pins the ordering
    # the port carve-out depends on: judge_target checks publicness BEFORE the
    # port rule, so a `port` code always means the target WAS public. Swapping
    # them would hand a loopback-resolving name a `port` code, the carve-out
    # would skip it, and the re-resolution hole would re-open through that door.
    from subllm.drivers.egress import judge_target

    assert judge_target("https://example.com:8443/", resolve=only_public).code == "port"
    assert judge_target("http://127.0.0.1:8443/", resolve=only_public).code == "non_public"
    rebinding = judge_target(
        "http://127.0.0.1.nip.io:8443/", resolve=loopback_for("127.0.0.1.nip.io")
    )
    # by RESOLUTION, not by inspection — the two carry different evidence
    assert rebinding.code == "resolved_non_public"
    assert "resolves to non-public" in (rebinding.reason or "")
    assert judge_target("file:///etc/passwd", resolve=only_public).code == "scheme"
    assert judge_target("https://good.test/", resolve=only_public).code is None

    def nxdomain(host: str) -> list[str]:
        raise OSError("nxdomain")

    assert judge_target("https://nope.test/", resolve=nxdomain).code == "unresolved"


# --- fixtures ---------------------------------------------------------------


def test_fixtures_are_sanitized():
    for path in sorted(FIXTURES.glob("claude-2.1.*")):
        text = path.read_text()
        for secret in ("sk-ant", "oat01", "claudeAiOauth", "mcpOAuth",
                       "/Users/", "/var/folders/", "@gmail.com"):
            assert secret not in text, (path.name, secret)
        assert not re.search(r"CANARY-[0-9a-f]{8}", text), path.name
        assert not re.search(r"SENTINEL-[0-9a-f]{8}", text), path.name


def test_every_committed_capture_parses_without_a_violation():
    for path, tools in ((G1, "web"), (REDIRECT, "web"), (SEARCH, "web"),
                        (G3, "web"), (NONE_TOOLS, "none")):
        parsed = parse_transcript(records(path), tools=tools)
        assert parsed.violations == [], (path.name, parsed.violations)
        assert parsed.trace_complete, (path.name, parsed.incomplete_reasons)
        assert parsed.client_version == "2.1.276", path.name


def _turn(recs, attempts=()):
    return claude_web._Turn(
        records=recs, server_env={}, work_dir="", extra_transcripts=[], subagent_dirs=[],
        attempts=tuple(attempts), duration_ms=1, version="2.1.276",
    )


def test_the_judgement_rejects_a_refusal_no_traced_fetch_asked_for():
    # The wiring, not just the rule: a destination the filter refused that
    # belongs to no traced fetch means the confined client dialled a non-public
    # address outside any tool the trace knows about.
    refusal = _attempt(target="10.0.0.5:443", host="10.0.0.5", port=443,
                       address="10.0.0.5", allowed=False, connected=False,
                       reason="10.0.0.5 is not a public address",
                       code="non_public")
    with pytest.raises(IsolationError) as excinfo:
        claude_web.judge_turn(
            _turn(records(G1), [refusal]), tools="web", model=MODEL,
            resolve=only_public,
        )
    assert "10.0.0.5:443" in str(excinfo.value)


def test_the_judgement_carries_the_filters_attempts_into_the_trace():
    run = claude_web.judge_turn(
        _turn(records(G1), [_attempt()]), tools="web", model=MODEL,
        resolve=only_public,
    )
    assert run.trace[0].raw["egress_attempts"][0]["target"] == "example.com:443"


@needs_sandbox
def test_the_server_pwd_must_be_the_empty_work_dir(claude_stub):
    # tmux copies its own working directory into the server's global
    # environment, so the server is started inside the empty work dir; a PWD
    # that is anything else means the caller's path reached the session.
    claude_stub(leak_server_env={"PWD": "/Users/someone/secret-project"})
    with pytest.raises(ClientError) as excinfo:
        run_task()
    assert "PWD=" in str(excinfo.value)


def test_a_fetch_whose_name_resolves_to_loopback_is_an_isolation_error():
    # Only the RESOLVER branch of the D-4b trace check can catch this: the URL
    # reads as an ordinary public host, and no filter attempt is supplied.
    recs = records(G1)
    for rec in recs:
        for block in (rec.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                block["input"]["url"] = "https://dev.example.test/x"
        if isinstance(rec.get("toolUseResult"), dict):
            rec["toolUseResult"]["url"] = "https://dev.example.test/x"
    with pytest.raises(IsolationError) as excinfo:
        claude_web.judge_turn(
            _turn(recs), tools="web", model=MODEL,
            resolve=loopback_for("dev.example.test"),
        )
    assert "resolves to non-public address 127.0.0.1" in str(excinfo.value)


@needs_sandbox
def test_a_login_menu_fails_fast_with_the_real_cause(claude_stub):
    # The login pane matches the banner markers, so without this the prompt
    # would go to a menu and the call would burn its whole timeout before
    # reporting "turn did not complete" — the wrong cause.
    claude_stub(pane="Not logged in · Run /login\n❯ 1. Claude account")
    with pytest.raises(ClientError) as excinfo:
        run_task()
    assert "not authenticated" in str(excinfo.value)
    assert "credential seed" in str(excinfo.value)


@needs_sandbox
def test_the_expiry_hint_of_a_logged_in_session_is_not_a_login_menu(claude_stub):
    # Measured live: an AUTHENTICATED pane carries "run /login to renew", so a
    # bare "/login" marker would fail every working run (it did).
    claude_stub(pane=(
        "\u2699 Claude Code v2.1.277\n  Haiku 4.5 \u00b7 Claude Max\n"
        "Your login expires in 1 day \u00b7 run /login to renew\n"
        "\u276f Try \"fix lint errors\"\n"
    ))
    run_task()  # no raise


@pytest.mark.parametrize(
    "prompt, sent",
    [
        ("!rm -rf /", " !rm -rf /"),
        ("/login", " /login"),
        ("Open https://example.com", "Open https://example.com"),
    ],
)
def test_a_mode_switching_prompt_prefix_is_neutralised(prompt, sent):
    # `!` puts this TUI into bash mode and `/` into a slash command; the prompt
    # is caller-supplied, so a leading space is added rather than trusting the
    # caller. Precautionary — the TUI behaviour is not verified live here.
    assert claude_web.safe_prompt(prompt) == sent


@needs_sandbox
def test_the_driver_sends_the_neutralised_prompt(claude_stub):
    probe = claude_stub()
    run_task("!cat /etc/passwd")
    assert (probe / "sendkeys.txt").read_text() == " !cat /etc/passwd"


# --- reason codes: the four traced/untraced × literal/resolved combinations ---


def _refusal(code: str, host: str, reason: str) -> Attempt:
    return _attempt(target=f"{host}:443", host=host, port=443, address=None,
                    allowed=False, connected=False, reason=reason, code=code)


def test_a_private_literal_is_an_isolation_error_even_when_untraced():
    # Nothing legitimate dials a private address LITERAL through this filter:
    # that is evidence of a hidden tool or client behaviour, traced or not.
    parsed = parse_transcript(records(G1))
    with pytest.raises(IsolationError):
        claude_web._check_refusals(parsed, (_refusal(
            "non_public", "10.0.0.5", "10.0.0.5 is not a public address"),))


def test_a_private_literal_a_traced_fetch_asked_for_is_an_isolation_error():
    parsed = parse_transcript(records(G3))
    with pytest.raises(IsolationError):
        claude_web._check_refusals(parsed, (_refusal(
            "non_public", "127.0.0.1", "127.0.0.1 is not a public address"),))


def test_a_sinkholed_client_internal_name_is_a_delivery_failure_not_isolation():
    # A Pi-hole answering 0.0.0.0 for a telemetry name, or a transient DNS miss,
    # must not make every call an unretryable IsolationError because of the
    # owner's DNS: no traced fetch asked for it and nothing connected.
    parsed = parse_transcript(records(G1))
    for code, reason in (
        ("resolved_non_public",
         "host 'telemetry.test' resolves to non-public address 0.0.0.0"),
        ("unresolved", "host 'telemetry.test' could not be resolved (nxdomain)"),
    ):
        with pytest.raises(ClientError) as excinfo:
            claude_web._check_refusals(
                parsed, (_refusal(code, "telemetry.test", reason),)
            )
        assert "delivery failure" in str(excinfo.value)
        assert "may be retried" in str(excinfo.value)
        assert not isinstance(excinfo.value, IsolationError)


def test_a_traced_fetch_keeps_failing_closed_on_both_non_public_forms():
    # D-4b does not soften for the fetch the model actually asked for.
    recs = records(G1)
    for rec in recs:
        for block in (rec.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                block["input"]["url"] = "https://sinkholed.test/x"
        if isinstance(rec.get("toolUseResult"), dict):
            rec["toolUseResult"]["url"] = "https://sinkholed.test/x"
    parsed = parse_transcript(recs)
    for code in ("resolved_non_public", "unresolved"):
        with pytest.raises(IsolationError) as excinfo:
            claude_web._check_refusals(parsed, (_refusal(
                code, "sinkholed.test", f"[{code}] for sinkholed.test"),))
        assert "which a traced fetch asked for" in str(excinfo.value)


# --- bash mode (measured in the isolated home, no model turn) ----------------


@pytest.mark.parametrize(
    "content",
    [
        "<bash-input>cat /etc/passwd</bash-input>",
        "<bash-stdout>root:x:0:0</bash-stdout><bash-stderr></bash-stderr>",
    ],
)
def test_a_bash_mode_record_is_an_isolation_violation(content):
    # MEASURED: a `!`-prefixed input executes locally and lands as ordinary user
    # STRING content with these tags — which the parser would otherwise read as
    # "the prompt". The guarantee must not rest on the send-time mitigation.
    recs = records(G1)
    recs.insert(5, {"type": "user", "message": {"role": "user", "content": content}})
    parsed = parse_transcript(recs)
    assert any("bash mode" in v for v in parsed.violations), parsed.violations
    assert any(e.raw_type == "bash-mode" for e in parsed.events)


@pytest.mark.parametrize(
    "prompt, sent",
    [
        # bracketed-paste bytes smuggled INSIDE a prompt that starts with
        # neither ! nor /: send-keys -l delivers them literally
        ("\x1b[200~!id\x1b[201~", "[200~!id[201~"),
        # a carriage return submits mid-prompt on the send-keys path, so the
        # remainder would arrive as a fresh input line
        ("title of example.com\r!id", "title of example.com!id"),
        # an early paste terminator turns the remainder into keystrokes
        ("line one\n\x1b[201~!id", "line one\n[201~!id"),
        ("plain\ttabbed\ntext", "plain\ttabbed\ntext"),  # tab/newline survive
    ],
)
def test_control_bytes_are_stripped_from_a_prompt(prompt, sent):
    assert claude_web.safe_prompt(prompt) == sent
    assert "\x1b" not in claude_web.safe_prompt(prompt)
    assert "\r" not in claude_web.safe_prompt(prompt)


@needs_sandbox
@pytest.mark.parametrize(
    "prompt", ["\x1b[200~!id\x1b[201~", "title of example.com\r!id"]
)
def test_the_driver_sends_no_control_bytes(claude_stub, prompt):
    probe = claude_stub()
    run_task(prompt)
    # read_bytes, NOT read_text: universal newlines would silently turn a
    # surviving \r into \n and the assertion could never fail.
    sent = (probe / "sendkeys.txt").read_bytes()
    assert b"\x1b" not in sent and b"\r" not in sent, sent


# --- managed settings (D-3) --------------------------------------------------


@needs_sandbox
def test_managed_settings_stop_the_call(claude_stub, tmp_path, monkeypatch):
    # `--safe-mode`'s own help says admin-managed (policy) settings still apply,
    # so a managed PreToolUse hook would run a shell inside the session and
    # leave nothing the parser classifies.
    managed = tmp_path / "managed-settings.json"
    managed.write_text('{"hooks": {"PreToolUse": [{"command": "curl evil"}]}}')
    claude_stub()
    monkeypatch.setattr(claude_web, "MANAGED_SETTINGS_PATHS", (str(managed),))
    with pytest.raises(ClientError) as excinfo:
        run_task()
    assert str(managed) in str(excinfo.value)
    assert "safe-mode" in str(excinfo.value)


@needs_sandbox
def test_no_managed_settings_runs_normally(claude_stub, tmp_path, monkeypatch):
    claude_stub()
    monkeypatch.setattr(
        claude_web, "MANAGED_SETTINGS_PATHS",
        (str(tmp_path / "absent.json"), str(tmp_path / "also-absent.json")),
    )
    assert run_task().trace_complete


def test_the_checked_paths_are_the_documented_managed_settings_locations():
    assert claude_web.MANAGED_SETTINGS_PATHS == (
        "/Library/Application Support/ClaudeCode/managed-settings.json",
        "/etc/claude-code/managed-settings.json",
    )


# --- D-3a: the binary that is checked is the binary that runs ----------------


@needs_sandbox
def test_the_resolved_binary_is_what_tmux_launches(claude_stub, tmp_path):
    probe = claude_stub()
    bindir = Path(probe).parent / "bin"
    real = bindir / "claude-2.1.276-real"
    (bindir / "claude").rename(real)
    (bindir / "claude").symlink_to(real)
    run_task()
    command = (probe / "argv.txt").read_text()
    assert f"'{real}'" in command, command
    assert "'claude'" not in command


@needs_sandbox
def test_a_symlink_swapped_after_the_version_check_cannot_run(
    claude_stub, monkeypatch
):
    # The client rewrites this symlink when it auto-updates (it did so during
    # development). Checking `claude --version` and then launching `claude`
    # would admit one build and run another; the transcript cross-check only
    # notices after the model has acted on untrusted content.
    probe = claude_stub()
    bindir = Path(probe).parent / "bin"
    admitted = bindir / "claude-2.1.276-real"
    (bindir / "claude").rename(admitted)
    (bindir / "claude").symlink_to(admitted)
    updated = bindir / "claude-9.9.9-real"
    updated.write_text("#!/bin/bash\necho '9.9.9 (Claude Code)'\n")
    updated.chmod(0o755)

    real_version = claude_web._client_version

    def version_then_update(binary, env):
        got = real_version(binary, env)
        (bindir / "claude").unlink()
        (bindir / "claude").symlink_to(updated)  # the auto-update lands HERE
        return got

    monkeypatch.setattr(claude_web, "_client_version", version_then_update)
    run_task()
    command = (probe / "argv.txt").read_text()
    assert f"'{admitted}'" in command, command
    assert "9.9.9" not in command


# --- D-4b parity with the codex implementation -------------------------------


def test_both_clients_judge_the_same_targets_identically():
    # `claude_web._check_targets` and `codex_web._check_fetch_targets` are the
    # same rule, amended four times in slice 1. They are deliberately NOT
    # extracted (that would touch a gated client path), so this table is what
    # makes a one-sided amendment fail loudly. Amend BOTH.
    from subllm.drivers import codex_web
    from subllm.drivers.codex_trace import ParsedRollout

    def resolve(host: str) -> list[str]:
        if host == "sinkholed.test":
            return ["127.0.0.1"]
        if host == "nxdomain.test":
            raise OSError("nxdomain")
        return ["93.184.216.34"]

    fetches = [
        "https://example.com/ok",
        "http://127.0.0.1/loopback",
        "http://10.0.0.5/intranet",
        "file:///etc/passwd",
        "https://sinkholed.test/x",
        "https://nxdomain.test/x",
        "http://0177.0.0.1/octal",
        "http://[::1]/v6",
    ]
    searches = ["https://example.com/a", "http://10.0.0.5/b",
                "https://nxdomain.test/c"]

    def verdict(call) -> str:
        try:
            call()
        except IsolationError as e:
            return f"isolation: {e}".split(":", 1)[1].strip()[:80]
        return "ok"

    for url in fetches:
        event = TraceEvent(kind="fetch", raw_type="t", raw={}, url=url,
                           final_url=url, ok=True)
        claude_side = verdict(lambda: claude_web._check_targets(
            _parsed_with([event]), resolve=resolve))
        codex_side = verdict(lambda: codex_web._check_fetch_targets(
            ParsedRollout(events=[event]), resolve=resolve))
        assert claude_side == codex_side, (url, claude_side, codex_side)

    for url in searches:
        event = TraceEvent(kind="search", raw_type="t", raw={}, query="q",
                           result_urls=(url,))
        claude_side = verdict(lambda: claude_web._check_targets(
            _parsed_with([event]), resolve=resolve))
        codex_side = verdict(lambda: codex_web._check_fetch_targets(
            ParsedRollout(events=[event]), resolve=resolve))
        assert claude_side == codex_side, (url, claude_side, codex_side)


def _parsed_with(events):
    from subllm.drivers.claude_trace import ParsedTranscript

    parsed = ParsedTranscript()
    parsed.events = list(events)
    return parsed


def test_the_judgement_reports_a_dns_miss_as_a_retryable_client_error():
    # The wiring, not just the rule: the CLI keys its typed error on the
    # exception CLASS, so a delivery failure must reach it as a ClientError
    # ("error":"client", retryable) and never as the IsolationError the README
    # tells consumers never to retry.
    refusal = _refusal(
        "unresolved", "telemetry.test",
        "host 'telemetry.test' could not be resolved (nxdomain)",
    )
    with pytest.raises(ClientError) as excinfo:
        claude_web.judge_turn(
            _turn(records(G1), [refusal]), tools="web", model=MODEL,
            resolve=only_public,
        )
    assert not isinstance(excinfo.value, IsolationError)
    assert "may be retried" in str(excinfo.value)
