import json

import pytest

from subllm import IsolationError, TraceEvent, WebAgentResult
from subllm.errors import SubllmError
from subllm.webagent import derive_retrieved_urls, schema_model_from_json_schema


def _fetch(url, final_url, ok=True, raw_type="web.fetch"):
    return TraceEvent(
        kind="fetch",
        raw_type=raw_type,
        raw={"u": url},
        url=url,
        final_url=final_url,
        ok=ok,
    )


def _build(trace, **kw):
    kw.setdefault("client", "codex")
    kw.setdefault("model", "gpt-5.6-luna")
    kw.setdefault("answer", "hi")
    kw.setdefault("fetch_events_exposed", True)
    kw.setdefault("trace_complete", True)
    kw.setdefault("tools", "web")
    kw.setdefault("client_version", "0.154.0")
    kw.setdefault("duration_ms", 10)
    return WebAgentResult.build(trace=trace, **kw)


def test_retrieved_urls_use_final_url_dedup_first_seen():
    r = _build([
        _fetch("https://a.example/x", "https://a.example/final"),
        TraceEvent(kind="search", raw_type="web.search", raw={}, query="q"),
        _fetch("https://b.example", "https://b.example"),
        _fetch("https://a.example/x", "https://a.example/final"),
    ])
    assert r.retrieved_urls == ("https://a.example/final", "https://b.example")


def test_failed_fetch_is_not_retrieved():
    r = _build([_fetch("https://a.example", "https://a.example", ok=False)])
    assert r.retrieved_urls == ()


def test_fetch_with_unknown_success_is_not_retrieved():
    r = _build([_fetch("https://a.example", "https://a.example", ok=None)])
    assert r.retrieved_urls == ()


def test_fetch_without_final_url_is_not_retrieved():
    # D-4: consumers must not ground a site against a requested address alone.
    r = _build([_fetch("https://a.example", None)])
    assert r.retrieved_urls == ()


def test_unexposed_fetch_events_force_empty_retrieved():
    r = _build(
        [_fetch("https://a.example", "https://a.example")],
        fetch_events_exposed=False,
    )
    assert r.retrieved_urls == ()


def test_incomplete_trace_forces_empty_retrieved():
    r = _build(
        [_fetch("https://a.example", "https://a.example")],
        trace_complete=False,
    )
    assert r.retrieved_urls == ()


def _direct(**kw):
    base = dict(
        client="codex",
        model="m",
        answer="hi",
        trace=(),
        retrieved_urls=(),
        fetch_events_exposed=True,
        trace_complete=True,
        tools="web",
        client_version="v",
        duration_ms=1,
    )
    base.update(kw)
    return WebAgentResult(**base)


def test_direct_construction_rejects_urls_when_fetch_events_not_exposed():
    with pytest.raises(ValueError):
        _direct(fetch_events_exposed=False, retrieved_urls=("https://a.example",))


def test_direct_construction_rejects_urls_when_trace_incomplete():
    with pytest.raises(ValueError):
        _direct(trace_complete=False, retrieved_urls=("https://a.example",))


def test_direct_construction_rejects_undeduped_or_reordered_urls():
    trace = (
        _fetch("https://a.example", "https://a.example"),
        _fetch("https://b.example", "https://b.example"),
    )
    with pytest.raises(ValueError):
        _direct(trace=trace, retrieved_urls=("https://b.example", "https://a.example"))
    with pytest.raises(ValueError):
        _direct(
            trace=trace,
            retrieved_urls=(
                "https://a.example",
                "https://b.example",
                "https://a.example",
            ),
        )


def test_direct_construction_rejects_failed_or_unknown_final_url():
    with pytest.raises(ValueError):
        _direct(
            trace=(_fetch("https://a.example", "https://a.example", ok=False),),
            retrieved_urls=("https://a.example",),
        )
    with pytest.raises(ValueError):
        _direct(
            trace=(_fetch("https://a.example", None),),
            retrieved_urls=("https://a.example",),
        )


def test_empty_final_url_is_not_retrieved():
    r = _build([_fetch("https://a.example", "")])
    assert r.retrieved_urls == ()
    with pytest.raises(ValueError):
        _direct(trace=(_fetch("https://a.example", ""),), retrieved_urls=("",))


def test_sequences_are_coerced_to_tuples_and_owned():
    events = [_fetch("https://a.example", "https://a.example")]
    r = _direct(
        trace=events,
        retrieved_urls=["https://a.example"],
        duration_ms="7",
    )
    events.clear()  # a caller's later mutation must not reach the result
    assert r.trace == (_fetch("https://a.example", "https://a.example"),)
    assert r.retrieved_urls == ("https://a.example",)
    assert r.duration_ms == 7
    assert r.to_json()["retrieved_urls"] == ["https://a.example"]


def test_event_result_urls_are_coerced_to_a_tuple():
    urls = ["https://a.example"]
    ev = TraceEvent(kind="search", raw_type="s", raw={}, query="q", result_urls=urls)
    urls.clear()
    assert ev.result_urls == ("https://a.example",)


def test_isolation_error_rejects_a_non_trace_element():
    with pytest.raises(TypeError):
        IsolationError("x", ["notevent"])


def test_exotic_raw_records_serialize():
    from datetime import datetime

    ev = TraceEvent(
        kind="other",
        raw_type="weird",
        raw={"b": b"ab", "t": datetime(2026, 1, 2), "n": {1, 2}},
    )
    payload = ev.to_json()["raw"]
    assert payload["b"] == "ab"
    assert payload["t"] == "2026-01-02 00:00:00"
    assert sorted(payload["n"]) == [1, 2]
    json.dumps(payload)


def test_derive_helper_matches_build():
    trace = [_fetch("https://a.example", "https://a.example")]
    assert derive_retrieved_urls(trace) == ("https://a.example",)


def test_event_json_shapes_are_kind_driven():
    search = TraceEvent(
        kind="search",
        raw_type="web_search",
        raw={"k": 1},
        query="q",
        result_urls=("https://a.example",),
    ).to_json()
    assert search == {
        "kind": "search",
        "query": "q",
        "result_urls": ["https://a.example"],
        "raw_type": "web_search",
        "raw": {"k": 1},
    }
    fetch = _fetch("https://a.example", None).to_json()
    assert fetch["final_url"] is None  # unknown final URL is marked, not omitted
    assert set(fetch) == {"kind", "url", "final_url", "ok", "raw_type", "raw"}
    other = TraceEvent(kind="other", raw_type="Bash", raw={"cmd": "ls"}).to_json()
    assert other == {"kind": "other", "raw_type": "Bash", "raw": {"cmd": "ls"}}


def test_isolation_error_is_a_subllm_error_carrying_the_trace():
    ev = TraceEvent(kind="other", raw_type="Bash", raw={})
    e = IsolationError("non-web tool", [ev])
    assert isinstance(e, SubllmError)
    assert e.trace == (ev,)
    assert str(e) == "non-web tool"
    assert IsolationError("x").trace == ()


def test_schema_model_exposes_the_document_and_validates_dicts():
    doc = {"type": "object", "properties": {"title": {"type": "string"}}}
    model = schema_model_from_json_schema(doc)
    assert model.model_json_schema() == doc
    assert model.model_validate({"title": "t"}).model_dump() == {"title": "t"}
    doc["properties"] = {}  # the model kept its own copy
    assert model.model_json_schema()["properties"] == {"title": {"type": "string"}}
