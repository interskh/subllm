"""Hermetic tests for codex web-agent mode: no network, no real `codex`.

The trace fixtures are live captures from codex-cli 0.154.0 / gpt-5.6-luna,
sanitized (see test_fixtures_are_sanitized). The fake `codex` on PATH answers
`--version` and `features list` from fixtures and, for `exec`, copies a chosen
rollout fixture into the throwaway CODEX_HOME exactly as the real client does.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from subllm.codex import CodexLLM
from subllm.drivers import codex_trace, codex_web
from subllm.drivers.codex_trace import parse_rollout
from subllm.errors import ClientError, IsolationError, QuotaError
from subllm.publictargets import classify_url
from subllm.webagent import derive_retrieved_urls

FIXTURES = Path(__file__).parent / "fixtures"
G1 = FIXTURES / "codex-0.154.0-open-success-and-failure.jsonl"
SEARCH = FIXTURES / "codex-0.154.0-search.jsonl"
G3 = FIXTURES / "codex-0.154.0-g3-negative-control.jsonl"
FEATURES = FIXTURES / "codex-0.154.0-features-list.txt"
SUBAGENT_PARENT = FIXTURES / "codex-0.154.0-subagent-parent.jsonl"
SUBAGENT_CHILD = FIXTURES / "codex-0.154.0-subagent-child.jsonl"

ADMITTED_MODEL = "gpt-5.6-luna"


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_rollout(path: Path, recs: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    return path


def only_public(host: str) -> list[str]:
    """Injected resolver: every hostname is public. Keeps tests off DNS."""
    return ["93.184.216.34"]


@pytest.fixture
def codex_stub(fake_bin, tmp_path, monkeypatch):
    """Install a fake `codex`. Returns a callable that (re)configures it and a
    dict of probe paths."""
    env_log = tmp_path / "child-env.txt"
    argv_log = tmp_path / "child-argv.txt"
    home_src = tmp_path / "real-codex-home"
    home_src.mkdir()
    (home_src / "auth.json").write_text('{"tokens": {"access_token": "secret"}}')
    monkeypatch.setenv("CODEX_HOME", str(home_src))

    probes = {"env": env_log, "argv": argv_log}

    def configure(
        *,
        rollouts: list[Path] = (),
        version: str = "0.154.0",
        features: Path = FEATURES,
        answer: str = "the answer",
        exit_code: int = 0,
        stderr: str = "",
    ) -> dict:
        copies = "\n".join(
            f'cp "{src}" "$CODEX_HOME/sessions/2026/09/18/rollout-{i}-x.jsonl"'
            for i, src in enumerate(rollouts)
        )
        fake_bin("codex", f'''#!/bin/bash
case "$1" in
  --version) echo "codex-cli {version}"; exit 0;;
  features) cat "{features}"; exit 0;;
esac
env > "{env_log}"
printf '%s\\n' "$@" > "{argv_log}"
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
mkdir -p "$CODEX_HOME/sessions/2026/09/18"
{copies}
printf '%s' "{answer}" > "$out"
printf '%s' "{stderr}" >&2
exit {exit_code}
''')
        return probes

    return configure


def run_task(prompt: str = "go", *, tools: str = "web", model: str = ADMITTED_MODEL,
             resolve=only_public):
    return codex_web.run_codex_web_task(
        prompt, model=model, tools=tools, resolve=resolve
    )


# --- trace parsing (D-4) ---------------------------------------------------


def test_g1_fixture_yields_one_retrieved_page_and_one_failed_fetch(codex_stub):
    codex_stub(rollouts=[G1])
    run = run_task()
    fetches = [e for e in run.trace if e.kind == "fetch"]
    assert [(e.url, e.final_url, e.ok) for e in fetches] == [
        ("https://example.com", "https://example.com/", True),
        ("https://example.com/definitely-missing-subllm-probe", None, False),
    ]
    assert run.trace_complete
    assert run.client_version == "0.154.0"
    # The failed fetch is NOT retrieved, and the retrieved URL is the FINAL one.
    assert derive_retrieved_urls(run.trace) == ("https://example.com/",)


def test_search_fixture_yields_a_search_event_and_no_retrieved_urls(codex_stub):
    codex_stub(rollouts=[SEARCH])
    run = run_task()
    searches = [e for e in run.trace if e.kind == "search"]
    assert len(searches) == 1
    assert searches[0].query == "IANA reserved example domains"
    assert "https://www.iana.org/domains/reserved" in searches[0].result_urls
    # Search results are never retrieved pages (D-4).
    assert derive_retrieved_urls(run.trace) == ()
    assert run.trace_complete


def test_events_keep_their_raw_client_record(codex_stub):
    codex_stub(rollouts=[G1])
    run = run_task()
    event = run.trace[0]
    assert event.raw_type == "web.search/openPage"
    assert event.raw["payload"]["item"]["kind"] == "web.search"


def test_multi_open_pairs_by_index_not_by_action_url():
    # action.url carries only the FIRST ref_id of a multi-open call, so a parser
    # that used it would silently drop the second fetch.
    parsed = parse_rollout(records(G1))
    assert len({e.url for e in parsed.events if e.kind == "fetch"}) == 2


# --- trace completeness (D-4) ----------------------------------------------


def test_zero_web_events_on_a_web_call_is_incomplete(tmp_path, codex_stub):
    # Measured: with the web tool removed the model answers from memory and the
    # answer looks right. This rule is what stops silent false grounding.
    recs = [r for r in records(G1) if (r.get("payload") or {}).get("type") != "item_completed"
            or (r["payload"].get("item") or {}).get("kind") != "web.search"]
    rollout = write_rollout(tmp_path / "no-web.jsonl", recs)
    codex_stub(rollouts=[rollout])
    run = run_task()
    assert not run.trace_complete
    assert derive_retrieved_urls(run.trace) == ()


@pytest.mark.parametrize(
    "record",
    [
        {"type": "weather_update", "payload": {"sky": "blue"}},
        # codex's own item types: none of these is on the non-tool allowlist,
        # and a name-pattern heuristic let FileChange through as "incomplete".
        {"type": "event_msg", "payload": {"type": "item_completed",
                                          "item": {"type": "FileChange"}}},
        {"type": "event_msg", "payload": {"type": "item_completed",
                                          "item": {"type": "WebSearch"}}},
        {"type": "event_msg", "payload": {"type": "item_completed",
                                          "item": {"type": "Extension",
                                                   "kind": "apps"}}},
        {"type": "event_msg", "payload": {"type": "exec_command_begin"}},
    ],
)
def test_unrecognised_record_is_an_isolation_violation(tmp_path, codex_stub, record):
    recs = records(G1)
    recs.insert(3, record)
    rollout = write_rollout(tmp_path / "unknown.jsonl", recs)
    codex_stub(rollouts=[rollout])
    with pytest.raises(IsolationError) as excinfo:
        CodexLLM(model=ADMITTED_MODEL).run_web_task("go")
    assert excinfo.value.trace, "the violation record must reach the caller"


def test_unknown_evidence_that_cannot_be_a_tool_only_marks_it_incomplete(
    tmp_path, codex_stub
):
    # §5's "other unrecognised event -> incomplete trace with empty retrieved
    # list": the record IS a recognised web item, only its action is unknown, so
    # no tool invocation can be hiding in it.
    recs = records(G1)
    for r in recs:
        item = (r.get("payload") or {}).get("item") or {}
        if item.get("kind") == "web.search":
            item["action"]["type"] = "teleport"
    rollout = write_rollout(tmp_path / "unknown-action.jsonl", recs)
    codex_stub(rollouts=[rollout])
    result = CodexLLM(model=ADMITTED_MODEL).run_web_task("go")
    assert result.trace_complete is False
    # An incomplete trace yields an empty retrieved list BY CONSTRUCTION.
    assert result.retrieved_urls == ()


def test_truncated_stream_is_incomplete(tmp_path, codex_stub):
    recs = [r for r in records(G1)
            if (r.get("payload") or {}).get("type") != "task_complete"]
    rollout = write_rollout(tmp_path / "truncated.jsonl", recs)
    codex_stub(rollouts=[rollout])
    assert run_task().trace_complete is False


def test_unpaired_tool_call_is_incomplete(tmp_path, codex_stub):
    recs = [r for r in records(G1)
            if (r.get("payload") or {}).get("type") != "custom_tool_call_output"]
    rollout = write_rollout(tmp_path / "unpaired.jsonl", recs)
    codex_stub(rollouts=[rollout])
    assert run_task().trace_complete is False


def test_unknown_web_action_type_is_incomplete(tmp_path, codex_stub):
    recs = records(G1)
    for r in recs:
        item = (r.get("payload") or {}).get("item") or {}
        if item.get("kind") == "web.search":
            item["action"]["type"] = "teleport"
    rollout = write_rollout(tmp_path / "action.jsonl", recs)
    codex_stub(rollouts=[rollout])
    assert run_task().trace_complete is False


# --- isolation violations (D-5 / D-3a) -------------------------------------


def test_g3_negative_control_fixture_raises_isolation_error(codex_stub):
    codex_stub(rollouts=[G3])
    with pytest.raises(IsolationError) as excinfo:
        run_task()
    assert "apply_patch" in str(excinfo.value)
    assert excinfo.value.trace, "the isolation error must carry the trace"


@pytest.mark.parametrize(
    "script",
    [
        'await tools.apply_patch("x")',
        'const f = tools["apply_patch"]; await f("x")',
        'const {multi_agent_v1__spawn_agent} = tools;',
        'text(ALL_TOOLS.map(t => t.name).join())',
        'await fetch("http://127.0.0.1:1/")',
        'text(process.env.HOME)',
        'text(globalThis.x)',
        'eval("1+1")',
        'require("fs")',
        # ECMAScript identifier escapes: these RUN the real binding while
        # spelling nothing a literal-text scan matches.
        'await tools.\\u0061pply_patch(\"x\")',
        'text(\\u0041LL_TOOLS.length)',
        r'text(\u{70}rocess.env.HOME)',
        r'text(\x70rocess.env.HOME)',
        # A computed ref_id hides the requested target from the D-4b check.
        'await tools.web__run({open:[{ref_id:"file:"+"///tmp/x"}]})',
        'await tools.web__run({open:urls.map(u => ({ref_id:u}))})',
    ],
)
def test_script_scan_flags_every_non_web_binding_and_escape(tmp_path, codex_stub, script):
    recs = records(G1)
    for r in recs:
        if (r.get("payload") or {}).get("type") == "custom_tool_call":
            r["payload"]["input"] = script
    rollout = write_rollout(tmp_path / "script.jsonl", recs)
    codex_stub(rollouts=[rollout])
    with pytest.raises(IsolationError):
        run_task()


def test_web__run_script_is_not_a_violation(tmp_path, codex_stub):
    # The allowed binding must still pass, or the scan would block every call.
    codex_stub(rollouts=[G1])
    assert run_task().trace_complete


def test_a_legitimate_multi_open_script_still_passes_the_tightened_scan():
    # The G1 fixture's own script — literal ref_ids, no escapes, one binding.
    script = (
        'const r = await tools.web__run({open:[{ref_id:"https://example.com"},'
        '{ref_id:"https://example.com/x"}],response_length:"short"}); text(r)'
    )
    from subllm.drivers.codex_trace import open_ref_arrays, scan_script

    assert scan_script(script) == []
    assert open_ref_arrays(script) == [
        (["https://example.com", "https://example.com/x"], True)
    ]


def test_other_tool_call_record_types_are_violations(tmp_path, codex_stub):
    for ptype in ("function_call", "local_shell_call", "mcp_tool_call"):
        recs = records(G1)
        recs.insert(3, {"type": "response_item", "payload": {"type": ptype, "id": "x"}})
        rollout = write_rollout(tmp_path / f"{ptype}.jsonl", recs)
        codex_stub(rollouts=[rollout])
        with pytest.raises(IsolationError):
            run_task()


def test_custom_tool_call_with_another_name_is_a_violation(tmp_path, codex_stub):
    recs = records(G1)
    for r in recs:
        if (r.get("payload") or {}).get("type") == "custom_tool_call":
            r["payload"]["name"] = "shell"
    rollout = write_rollout(tmp_path / "named.jsonl", recs)
    codex_stub(rollouts=[rollout])
    with pytest.raises(IsolationError):
        run_task()


def test_spawned_subagent_is_caught_by_the_script_scan(codex_stub):
    # Layer 1, on the REAL parent capture: the spawn and wait calls are
    # non-web tool bindings.
    codex_stub(rollouts=[SUBAGENT_PARENT])
    with pytest.raises(IsolationError) as excinfo:
        run_task()
    assert "multi_agent_v1__spawn_agent" in str(excinfo.value)


def test_spawned_subagent_is_caught_by_the_rollout_count_alone(
    codex_stub, monkeypatch
):
    # Layer 2, proven independently: with the script scan stubbed out, the
    # child's own rollout in the same scratch home is what fails the call. The
    # child capture holds no tool call at all, so only this layer can see it.
    monkeypatch.setattr(codex_trace, "scan_script", lambda script: [])
    codex_stub(rollouts=[SUBAGENT_PARENT, SUBAGENT_CHILD])
    with pytest.raises(IsolationError) as excinfo:
        run_task()
    assert "sub-agent" in str(excinfo.value)


def test_the_child_capture_is_a_real_spawned_session():
    # Guards the fixture itself: it must be a genuine sub-agent rollout, not a
    # copy of the parent, or the test above would be circular.
    meta = records(SUBAGENT_CHILD)[0]["payload"]
    assert meta["thread_source"] == "subagent"
    assert meta["source"]["subagent"]["thread_spawn"]["depth"] == 1
    assert meta["parent_thread_id"] == records(SUBAGENT_PARENT)[0]["payload"]["id"]
    assert not [
        r for r in records(SUBAGENT_CHILD)
        if (r.get("payload") or {}).get("type") == "custom_tool_call"
    ], "the child must hold no tool call, or layer 2 is not tested alone"


def test_no_rollout_at_all_is_a_client_error(codex_stub):
    codex_stub(rollouts=[])
    with pytest.raises(ClientError):
        run_task()


# --- public targets (D-4b) -------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/canary",
        "data:text/plain,hi",
        "http://127.0.0.1:8932/x",
        "http://localhost:8932/x",
        "http://10.0.0.5/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/x",
        "http://[::1]/x",
        "http://224.0.0.1/x",
        # Alternate IP spellings: getaddrinfo reads 0177.0.0.1 as 177.0.0.1 on
        # macOS (public-looking) while the kernel dials loopback.
        "http://0177.0.0.1:8932/x",
        "http://0x7f000001/x",
        "http://2130706433/x",
    ],
)
def test_non_public_fetch_target_raises_isolation_error(tmp_path, codex_stub, url):
    recs = records(G1)
    for r in recs:
        item = (r.get("payload") or {}).get("item") or {}
        if item.get("kind") == "web.search":
            item["results"][0]["url"] = url
    rollout = write_rollout(tmp_path / "target.jsonl", recs)
    codex_stub(rollouts=[rollout])
    with pytest.raises(IsolationError) as excinfo:
        run_task()
    assert "non-public" in str(excinfo.value)


def test_public_looking_name_that_resolves_to_loopback_is_a_violation(
    tmp_path, codex_stub
):
    recs = records(G1)
    for r in recs:
        item = (r.get("payload") or {}).get("item") or {}
        if item.get("kind") == "web.search":
            item["results"][0]["url"] = "http://127.0.0.1.nip.io:8932/x"
    rollout = write_rollout(tmp_path / "nipio.jsonl", recs)
    codex_stub(rollouts=[rollout])
    def selective(host: str) -> list[str]:
        # ONLY the nip.io name resolves to loopback, so the requested URL
        # (example.com) passes and the final-URL branch is what fires.
        return ["127.0.0.1"] if "nip.io" in host else ["93.184.216.34"]

    with pytest.raises(IsolationError) as excinfo:
        run_task(resolve=selective)
    assert "final" in str(excinfo.value)


def test_non_public_requested_target_is_a_violation_even_when_the_fetch_failed(
    tmp_path, codex_stub
):
    # The requested address is only in the exec script's open: array. Here the
    # fetch failed (no results[1].url), so ONLY the requested-URL branch can
    # catch it — the client refused a file:// ref_id, but the trace still shows
    # the attempt and the answer must be withheld.
    recs = records(G1)
    for r in recs:
        payload = r.get("payload") or {}
        if payload.get("type") == "custom_tool_call":
            payload["input"] = payload["input"].replace(
                "https://example.com/definitely-missing-subllm-probe",
                "file:///tmp/canary",
            )
    rollout = write_rollout(tmp_path / "requested.jsonl", recs)
    codex_stub(rollouts=[rollout])
    with pytest.raises(IsolationError) as excinfo:
        run_task()
    assert "requested" in str(excinfo.value)


def test_requested_public_but_final_non_public_is_a_violation(tmp_path, codex_stub):
    recs = records(G1)
    for r in recs:
        item = (r.get("payload") or {}).get("item") or {}
        if item.get("kind") == "web.search":
            item["results"][0]["url"] = "http://192.168.1.10/admin"
    rollout = write_rollout(tmp_path / "redirect.jsonl", recs)
    codex_stub(rollouts=[rollout])
    with pytest.raises(IsolationError) as excinfo:
        run_task()
    assert "final" in str(excinfo.value)


def test_unresolvable_host_fails_closed():
    def boom(host: str) -> list[str]:
        raise OSError("no such host")

    assert classify_url("https://example.com/x", resolve=boom) is not None
    assert classify_url("https://example.com/x", resolve=only_public) is None


# --- admission (D-3a) and the feature inventory (D-2) ----------------------


def test_unadmitted_version_is_a_client_error_before_the_agent_runs(codex_stub):
    probes = codex_stub(rollouts=[G1], version="0.155.0")
    with pytest.raises(ClientError) as excinfo:
        run_task()
    assert "G3" in str(excinfo.value)
    assert not probes["argv"].exists(), "codex exec must not have run"


def test_unadmitted_model_is_a_client_error_before_the_agent_runs(codex_stub):
    probes = codex_stub(rollouts=[G1])
    with pytest.raises(ClientError):
        run_task(model="gpt-5.6-nova")
    assert not probes["argv"].exists(), "codex exec must not have run"


def test_missing_feature_switch_is_a_client_error(tmp_path, codex_stub):
    trimmed = tmp_path / "features.txt"
    trimmed.write_text(
        "\n".join(
            line for line in FEATURES.read_text().splitlines()
            if not line.startswith("browser_use ")
        )
        + "\n"
    )
    probes = codex_stub(rollouts=[G1], features=trimmed)
    with pytest.raises(ClientError) as excinfo:
        run_task()
    assert "browser_use" in str(excinfo.value)
    assert not probes["argv"].exists()


def test_unknown_enabled_feature_is_a_client_error(tmp_path, codex_stub):
    extended = tmp_path / "features.txt"
    extended.write_text(
        FEATURES.read_text() + "brand_new_capability                     stable             true\n"
    )
    probes = codex_stub(rollouts=[G1], features=extended)
    with pytest.raises(ClientError) as excinfo:
        run_task()
    assert "brand_new_capability" in str(excinfo.value)
    assert not probes["argv"].exists()


def test_a_disable_that_did_not_take_is_a_client_error(tmp_path, codex_stub):
    flipped = tmp_path / "features.txt"
    flipped.write_text(
        FEATURES.read_text().replace(
            "shell_tool                               stable             false",
            "shell_tool                               stable             true",
        )
    )
    codex_stub(rollouts=[G1], features=flipped)
    with pytest.raises(ClientError) as excinfo:
        run_task()
    assert "shell_tool" in str(excinfo.value)


def test_the_measured_inventory_passes(codex_stub):
    # Guards the allowlist itself: the committed 0.154.0 inventory must run.
    codex_stub(rollouts=[G1])
    assert run_task().trace_complete


# --- environment allowlist (D-6) -------------------------------------------


def test_child_environment_is_exactly_the_allowlist(codex_stub, monkeypatch):
    monkeypatch.setenv("SUBLLM_TEST_SECRET", "hunter2")
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@host/db")
    probes = codex_stub(rollouts=[G1])
    run_task()
    seen = dict(
        line.split("=", 1)
        for line in probes["env"].read_text().splitlines()
        if "=" in line
    )
    # bash itself adds these to what it was handed.
    observed = set(seen) - {"_", "PWD", "SHLVL", "OLDPWD"}
    # Nothing outside the allowlist reaches the child; PATH/HOME/CODEX_HOME are
    # always set (USER/LANG/TERM only when the caller has them).
    assert observed <= set(codex_web.ENV_ALLOWLIST)
    assert {"PATH", "HOME", "CODEX_HOME"} <= observed
    assert "hunter2" not in probes["env"].read_text()
    assert "DATABASE_URL" not in seen


def test_home_is_redirected_away_from_the_owners_home(codex_stub):
    probes = codex_stub(rollouts=[G1])
    run_task()
    seen = dict(
        line.split("=", 1)
        for line in probes["env"].read_text().splitlines()
        if "=" in line
    )
    assert seen["HOME"] != str(Path.home())
    assert "subllm-codex-home-" in seen["HOME"]
    assert seen["CODEX_HOME"] != seen["HOME"]


def test_every_scratch_directory_is_deleted_after_the_call(codex_stub):
    probes = codex_stub(rollouts=[G1])
    run_task()
    seen = dict(
        line.split("=", 1)
        for line in probes["env"].read_text().splitlines()
        if "=" in line
    )
    argv = probes["argv"].read_text().splitlines()
    work_dir = argv[argv.index("-C") + 1]
    for path in (seen["HOME"], seen["CODEX_HOME"], work_dir):
        assert not Path(path).exists(), f"{path} survived the call"


def test_existing_text_calls_still_pass_the_full_environment(fake_bin, tmp_path,
                                                             monkeypatch):
    from subllm.drivers.codex_exec import run_codex_exec

    monkeypatch.setenv("SUBLLM_TEST_SECRET", "hunter2")
    env_log = tmp_path / "text-env.txt"
    fake_bin("codex", f'''#!/bin/bash
env > "{env_log}"
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
printf 'ok' > "$out"
''')
    run_codex_exec("x", codex_home=str(tmp_path))
    assert "hunter2" in env_log.read_text()


# --- flags (D-2) -----------------------------------------------------------


def test_argv_carries_the_verified_flag_set(codex_stub):
    probes = codex_stub(rollouts=[G1])
    run_task()
    argv = probes["argv"].read_text().splitlines()
    for flag in ("exec", "-s", "read-only", "--skip-git-repo-check",
                 "--ignore-user-config", "--ignore-rules", "--json", "--"):
        assert flag in argv
    assert "project_doc_max_bytes=0" in argv
    assert 'web_search="live"' in argv
    # --ephemeral would suppress the rollout, which IS the trace.
    assert "--ephemeral" not in argv
    for feature in codex_web.DISABLED_FEATURES:
        assert feature in argv
    work_dir = argv[argv.index("-C") + 1]
    assert "subllm-codex-cwd-" in work_dir


def test_schema_is_passed_and_the_answer_is_validated(codex_stub, tmp_path):
    from pydantic import BaseModel

    class Answer(BaseModel):
        title: str

    probes = codex_stub(rollouts=[G1], answer='{\\"title\\": \\"Example Domain\\"}')
    llm = CodexLLM(model=ADMITTED_MODEL)
    result = llm.run_web_task("go", Answer)
    assert result.answer == {"title": "Example Domain"}
    assert "--output-schema" in probes["argv"].read_text()


def test_tools_none_disables_web_search_explicitly(tmp_path, codex_stub):
    # Measured: merely omitting web_search leaves the tool offered AND working.
    recs = [r for r in records(G1)
            if (r.get("payload") or {}).get("type") not in
            ("custom_tool_call", "custom_tool_call_output")
            and ((r.get("payload") or {}).get("item") or {}).get("kind") != "web.search"]
    rollout = write_rollout(tmp_path / "quiet.jsonl", recs)
    probes = codex_stub(rollouts=[rollout])
    run = run_task(tools="none")
    assert 'web_search="disabled"' in probes["argv"].read_text()
    assert run.trace == ()
    assert run.trace_complete


def test_tools_none_treats_any_tool_event_as_a_violation(codex_stub):
    codex_stub(rollouts=[G1])
    with pytest.raises(IsolationError) as excinfo:
        run_task(tools="none")
    assert "tools='none'" in str(excinfo.value)


# --- errors ----------------------------------------------------------------


def test_quota_stderr_maps_to_quota_error(codex_stub):
    codex_stub(rollouts=[], exit_code=1, stderr="You have hit your usage limit.")
    with pytest.raises(QuotaError):
        run_task()


def test_non_zero_exit_maps_to_client_error(codex_stub):
    codex_stub(rollouts=[], exit_code=1, stderr="Authentication failed.")
    with pytest.raises(ClientError):
        run_task()


def test_run_web_task_returns_the_contract_result(codex_stub):
    codex_stub(rollouts=[G1])
    result = CodexLLM(model=ADMITTED_MODEL).run_web_task("go")
    document = result.to_json()
    assert document["client"] == "codex"
    assert document["model"] == ADMITTED_MODEL
    assert document["tools"] == "web"
    assert document["retrieved_urls"] == ["https://example.com/"]
    assert document["fetch_events_exposed"] is True
    assert document["trace_complete"] is True
    assert document["client_version"] == "0.154.0"
    assert document["duration_ms"] >= 0


def test_run_web_task_without_a_model_is_a_client_error():
    with pytest.raises(ClientError):
        CodexLLM().run_web_task("go")


# --- fixtures --------------------------------------------------------------


def test_fixtures_are_sanitized():
    for path in sorted(FIXTURES.iterdir()):
        body = path.read_text()
        for needle in ("/Users/", "access_token", "installation_id",
                       "base_instructions", "OPENAI_API_KEY", "id_token"):
            assert needle not in body, f"{path.name} still holds {needle}"
