import io
import json
from datetime import datetime

import pytest

from subllm import cli
from subllm.errors import (
    ClientError,
    IsolationError,
    OutputError,
    QuotaError,
    RegionError,
)
from subllm.webagent import TraceEvent, WebAgentResult

SEARCH = TraceEvent(
    kind="search",
    raw_type="web_search",
    raw={"query": "example"},
    query="example",
    result_urls=(),
)
FETCH = TraceEvent(
    kind="fetch",
    raw_type="web_fetch",
    raw={"url": "https://example.com"},
    url="https://example.com",
    final_url="https://example.com/",
    ok=True,
)


class _StubClient:
    def __init__(self, *, result=None, raises=None):
        self._result = result
        self._raises = raises
        self.calls = []

    def run_web_task(self, prompt, schema_model=None, *, tools="web", **kwargs):
        self.calls.append({
            "prompt": prompt,
            "schema_model": schema_model,
            "tools": tools,
            **kwargs,
        })
        if self._raises is not None:
            raise self._raises
        return self._result


def _result(answer="Example Domain"):
    return WebAgentResult.build(
        client="codex",
        model="gpt-5.6-luna",
        answer=answer,
        trace=(SEARCH, FETCH),
        fetch_events_exposed=True,
        trace_complete=True,
        tools="web",
        client_version="codex-cli 0.154.0",
        duration_ms=12345,
    )


@pytest.fixture
def stub(monkeypatch):
    holder = {}

    def _install(**kw):
        client = _StubClient(**kw)
        holder["client"] = client
        monkeypatch.setattr(cli, "_build_web_agent_client", lambda name, model: client)
        return client

    return _install


def _run(argv, capsys):
    rc = cli.main(argv)
    out = capsys.readouterr()
    return rc, out


def test_success_prints_one_json_document(stub, capsys):
    stub(result=_result())
    rc, out = _run(
        ["web-agent", "--client", "codex", "--model", "gpt-5.6-luna", "title?"],
        capsys,
    )
    assert rc == 0
    assert out.out.count("\n") == 1
    doc = json.loads(out.out)
    assert doc == {
        "client": "codex",
        "model": "gpt-5.6-luna",
        "tools": "web",
        "answer": "Example Domain",
        "trace": [
            {
                "kind": "search",
                "query": "example",
                "result_urls": [],
                "raw_type": "web_search",
                "raw": {"query": "example"},
            },
            {
                "kind": "fetch",
                "url": "https://example.com",
                "final_url": "https://example.com/",
                "ok": True,
                "raw_type": "web_fetch",
                "raw": {"url": "https://example.com"},
            },
        ],
        "retrieved_urls": ["https://example.com/"],
        "fetch_events_exposed": True,
        "trace_complete": True,
        "client_version": "codex-cli 0.154.0",
        "duration_ms": 12345,
    }


def test_prompt_and_tools_and_timeout_reach_the_client(stub, capsys):
    client = stub(result=_result())
    rc, _ = _run(
        [
            "web-agent", "--client", "codex", "--model", "m",
            "--tools", "none", "--timeout", "30", "do it",
        ],
        capsys,
    )
    assert rc == 0
    assert client.calls == [
        {"prompt": "do it", "schema_model": None, "tools": "none", "timeout_s": 30.0}
    ]


def test_timeout_is_omitted_when_not_given(stub, capsys):
    client = stub(result=_result())
    _run(["web-agent", "--client", "codex", "--model", "m", "x"], capsys)
    assert "timeout_s" not in client.calls[0]


def test_prompt_from_stdin(stub, capsys, monkeypatch):
    client = stub(result=_result())
    monkeypatch.setattr("sys.stdin", io.StringIO("piped prompt"))
    rc, _ = _run(["web-agent", "--client", "codex", "--model", "m", "-"], capsys)
    assert rc == 0
    assert client.calls[0]["prompt"] == "piped prompt"


def test_schema_file_is_passed_as_a_schema_model_and_answer_stays_a_dict(
    stub, capsys, tmp_path
):
    document = {"type": "object", "properties": {"title": {"type": "string"}}}
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(document))
    client = stub(result=_result(answer={"title": "Example Domain"}))
    rc, out = _run(
        ["web-agent", "--client", "codex", "--model", "m", "--schema", str(path), "x"],
        capsys,
    )
    assert rc == 0
    schema_model = client.calls[0]["schema_model"]
    assert schema_model is not None
    assert schema_model.model_json_schema() == document
    assert json.loads(out.out)["answer"] == {"title": "Example Domain"}


def test_unreadable_schema_file_is_a_usage_error(stub, capsys, tmp_path):
    stub(result=_result())
    rc, out = _run(
        [
            "web-agent", "--client", "codex", "--model", "m",
            "--schema", str(tmp_path / "nope.json"), "x",
        ],
        capsys,
    )
    assert rc == 2
    assert out.out == ""
    assert "schema" in out.err


@pytest.mark.parametrize(
    "exc,name",
    [
        (QuotaError("cap reached"), "quota"),
        (ClientError("codex crashed"), "client"),
        (OutputError("bad json"), "output"),
        (RegionError("blocked region"), "region"),
    ],
)
def test_error_types_print_typed_json_on_stdout(stub, capsys, exc, name):
    stub(raises=exc)
    rc, out = _run(["web-agent", "--client", "codex", "--model", "m", "x"], capsys)
    assert rc != 0
    assert out.err == ""
    doc = json.loads(out.out)
    assert doc["error"] == name
    assert doc["message"] == str(exc)
    assert doc["trace"] is None


def test_isolation_error_carries_the_serialized_trace(stub, capsys):
    other = TraceEvent(kind="other", raw_type="shell", raw={"cmd": "ls"})
    stub(raises=IsolationError("non-web tool event", (FETCH, other)))
    rc, out = _run(["web-agent", "--client", "codex", "--model", "m", "x"], capsys)
    assert rc != 0
    doc = json.loads(out.out)
    assert doc["error"] == "isolation"
    assert doc["trace"] == [FETCH.to_json(), other.to_json()]
    assert doc["trace"][0]["url"] == "https://example.com"
    assert "answer" not in doc


def test_client_without_web_agent_mode_errors_without_a_traceback(capsys):
    # Goes through the REAL factory: ClaudeLLM has no run_web_task in slice 1.
    rc, out = _run(
        ["web-agent", "--client", "claude", "--model", "claude-haiku-4-5", "x"],
        capsys,
    )
    assert rc != 0
    doc = json.loads(out.out)
    assert doc["error"] == "client"
    assert doc["trace"] is None
    assert "web-agent" in doc["message"]


def test_unserializable_raw_record_still_yields_one_json_document(stub, capsys):
    # A client record may hold bytes/datetimes; §4.2 promises stdout parses.
    exotic = TraceEvent(
        kind="fetch",
        raw_type="web_fetch",
        raw={"body": b"\xff\xfe bytes", "at": datetime(2026, 9, 17, 12, 0)},
        url="https://example.com",
        final_url="https://example.com/",
        ok=True,
    )
    stub(result=WebAgentResult.build(
        client="codex", model="m", answer="a", trace=(exotic,),
        fetch_events_exposed=True, trace_complete=True, tools="web",
        client_version="v", duration_ms=1,
    ))
    rc, out = _run(["web-agent", "--client", "codex", "--model", "m", "x"], capsys)
    assert rc == 0
    doc = json.loads(out.out)
    assert doc["retrieved_urls"] == ["https://example.com/"]
    assert doc["trace"][0]["raw"]["at"] == "2026-09-17 12:00:00"
    assert isinstance(doc["trace"][0]["raw"]["body"], str)


def test_internal_failure_still_yields_one_typed_json_document(capsys, monkeypatch):
    # IsolationError refuses a non-TraceEvent trace; the resulting TypeError
    # must not reach the user as a traceback with empty stdout.
    class _Exploding:
        def run_web_task(self, prompt, schema_model=None, **kwargs):
            raise IsolationError("x", ["notevent"])

    monkeypatch.setattr(cli, "_build_web_agent_client", lambda n, m: _Exploding())
    rc, out = _run(["web-agent", "--client", "codex", "--model", "m", "x"], capsys)
    assert rc != 0
    assert out.err == ""
    doc = json.loads(out.out)
    assert doc["error"] == "client"
    assert doc["trace"] is None
    assert "TypeError" in doc["message"]


class _Unprintable:
    def __str__(self):
        raise RuntimeError("boom")

    __repr__ = __str__


def _assert_degraded_isolation(stub, capsys, raw):
    stub(raises=IsolationError("bad", [TraceEvent(kind="other", raw_type="x", raw=raw)]))
    rc, out = _run(["web-agent", "--client", "codex", "--model", "m", "x"], capsys)
    assert rc != 0
    assert out.err == ""
    doc = json.loads(out.out)  # exactly one parseable document
    assert doc["error"] == "isolation"  # the type survives; the trace degrades
    assert doc["message"] == "bad"
    assert doc["trace"] is None


def test_recursive_isolation_record_degrades_the_trace_not_the_document(stub, capsys):
    raw = {}
    raw["self"] = raw
    _assert_degraded_isolation(stub, capsys, raw)


def test_unprintable_isolation_record_degrades_the_trace_not_the_document(
    stub, capsys
):
    _assert_degraded_isolation(stub, capsys, {"o": _Unprintable()})


def test_surrogates_do_not_break_the_success_document(stub, capsys):
    ev = TraceEvent(
        kind="fetch",
        raw_type="web_fetch",
        raw={"path": "/tmp/\udcff", "score": float("nan")},
        url="https://example.com",
        final_url="https://example.com/",
        ok=True,
    )
    stub(result=WebAgentResult.build(
        client="codex", model="m", answer="a", trace=(ev,),
        fetch_events_exposed=True, trace_complete=True, tools="web",
        client_version="v", duration_ms=1,
    ))
    rc, out = _run(["web-agent", "--client", "codex", "--model", "m", "x"], capsys)
    assert rc == 0
    out.out.encode("utf-8")  # a strict-utf-8 consumer can read it
    doc = json.loads(out.out)
    raw = doc["trace"][0]["raw"]
    assert "\udcff" not in raw["path"]
    assert raw["score"] == "nan"  # strict JSON: no bare NaN token
    assert "NaN" not in out.out


def test_surrogates_in_an_error_message_still_yield_one_document(stub, capsys):
    stub(raises=ClientError("bad path /tmp/\udcff"))
    rc, out = _run(["web-agent", "--client", "codex", "--model", "m", "x"], capsys)
    assert rc != 0
    out.out.encode("utf-8")
    doc = json.loads(out.out)
    assert doc["error"] == "client"
    assert "\udcff" not in doc["message"]


def test_error_whose_str_raises_still_yields_one_document(stub, capsys):
    class _BadMessage(ClientError):
        def __str__(self):
            raise RuntimeError("boom")

    stub(raises=_BadMessage())
    rc, out = _run(["web-agent", "--client", "codex", "--model", "m", "x"], capsys)
    assert rc != 0
    assert out.err == ""
    doc = json.loads(out.out)
    assert doc["error"] == "client"
    assert doc["message"] == "<unprintable _BadMessage>"
    assert doc["trace"] is None


def test_write_json_line_survives_an_unencodable_document(capsys):
    cli._write_json_line('{"error": "client", "message": "\udcff"}')
    out = capsys.readouterr().out
    out.encode("utf-8")
    assert json.loads(out)["error"] == "client"


def test_non_object_schema_document_is_a_usage_error(stub, capsys, tmp_path):
    stub(result=_result())
    path = tmp_path / "s.json"
    path.write_text("42")
    rc, out = _run(
        ["web-agent", "--client", "codex", "--model", "m", "--schema", str(path), "x"],
        capsys,
    )
    assert rc == 2
    assert out.out == ""
    assert "JSON Schema object" in out.err


def test_bad_tools_value_is_rejected_by_argparse(capsys):
    rc, _ = _run(
        ["web-agent", "--client", "codex", "--model", "m", "--tools", "shell", "x"],
        capsys,
    )
    assert rc == 2
