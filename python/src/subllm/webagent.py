"""Shared web-agent types: the trace contract (D-4) and its JSON serialization.

`WebAgentResult` enforces the spec invariants by construction: an unexposed or
incomplete trace yields an empty retrieved list, and a retrieved URL is always
the FINAL url of a successful fetch, deduplicated in first-seen order.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from typing import Any, Iterable, Literal


def _clean_text(text: str) -> str:
    """Drop lone surrogates (a surrogateescape-decoded path can carry them),
    which no strict-UTF-8 stream can encode."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace").decode("utf-8")
    return text


def json_safe(value: Any) -> Any:
    """Coerce a value into something json.dumps accepts as STRICT JSON. A raw
    record may hold bytes, datetimes, non-finite floats, surrogates or other
    client objects; §4.2 promises one JSON document on stdout, so an exotic
    value becomes its string form rather than a serialization failure."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        # NaN/Infinity are not JSON — jq, JS and Go all reject them.
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, dict):
        return {_clean_text(str(k)): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return _clean_text(str(value))


@dataclass(frozen=True)
class TraceEvent:
    kind: Literal["search", "fetch", "other"]
    raw_type: str                     # client's own event/tool name
    raw: dict                         # the client's original record(s), unmodified
    query: str | None = None          # search
    url: str | None = None            # fetch: requested URL
    final_url: str | None = None      # fetch: after redirects, when exposed
    ok: bool | None = None            # fetch: client-reported success
    result_urls: tuple[str, ...] = () # search, when exposed

    def __post_init__(self) -> None:
        object.__setattr__(self, "result_urls", tuple(self.result_urls))

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        if self.kind == "search":
            out["query"] = self.query
            out["result_urls"] = list(self.result_urls)
        elif self.kind == "fetch":
            out["url"] = self.url
            out["final_url"] = self.final_url
            out["ok"] = self.ok
        out["raw_type"] = self.raw_type
        out["raw"] = json_safe(self.raw)
        return out


def derive_retrieved_urls(trace: Iterable[TraceEvent]) -> tuple[str, ...]:
    """Final URLs of successful fetches, dedup, first-seen order. A fetch whose
    final URL is unknown or empty is NOT retrieved (D-4: consumers must not
    ground against a requested address alone)."""
    urls: list[str] = []
    for ev in trace:
        if ev.kind != "fetch" or ev.ok is not True or not ev.final_url:
            continue
        if ev.final_url not in urls:
            urls.append(ev.final_url)
    return tuple(urls)


@dataclass(frozen=True)
class WebAgentResult:
    client: Literal["codex", "claude"]
    model: str | None
    answer: str | dict
    trace: tuple[TraceEvent, ...]
    retrieved_urls: tuple[str, ...]
    fetch_events_exposed: bool
    trace_complete: bool
    tools: Literal["web", "none"]
    client_version: str
    duration_ms: int

    def __post_init__(self) -> None:
        # Own the sequences: a caller's list stays mutable behind our back and
        # would break both the invariants below and tuple equality downstream.
        object.__setattr__(self, "trace", tuple(self.trace))
        object.__setattr__(self, "retrieved_urls", tuple(self.retrieved_urls))
        object.__setattr__(self, "duration_ms", int(self.duration_ms))
        if not self.fetch_events_exposed and self.retrieved_urls:
            raise ValueError(
                "retrieved_urls must be empty when fetch_events_exposed is False"
            )
        if not self.trace_complete and self.retrieved_urls:
            raise ValueError(
                "retrieved_urls must be empty when trace_complete is False"
            )
        if self.fetch_events_exposed and self.trace_complete:
            expected = derive_retrieved_urls(self.trace)
            if tuple(self.retrieved_urls) != expected:
                raise ValueError(
                    f"retrieved_urls must be {expected!r} derived from the trace, "
                    f"got {tuple(self.retrieved_urls)!r}"
                )

    @classmethod
    def build(
        cls,
        *,
        client: Literal["codex", "claude"],
        model: str | None,
        answer: str | dict,
        trace: Iterable[TraceEvent],
        fetch_events_exposed: bool,
        trace_complete: bool,
        tools: Literal["web", "none"],
        client_version: str,
        duration_ms: int,
    ) -> WebAgentResult:
        events = tuple(trace)
        retrieved = (
            derive_retrieved_urls(events)
            if fetch_events_exposed and trace_complete
            else ()
        )
        return cls(
            client=client,
            model=model,
            answer=answer,
            trace=events,
            retrieved_urls=retrieved,
            fetch_events_exposed=fetch_events_exposed,
            trace_complete=trace_complete,
            tools=tools,
            client_version=client_version,
            duration_ms=duration_ms,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "model": self.model,
            "tools": self.tools,
            "answer": json_safe(self.answer),
            "trace": [ev.to_json() for ev in self.trace],
            "retrieved_urls": list(self.retrieved_urls),
            "fetch_events_exposed": self.fetch_events_exposed,
            "trace_complete": self.trace_complete,
            "client_version": self.client_version,
            "duration_ms": self.duration_ms,
        }


def schema_model_from_json_schema(schema: dict[str, Any]) -> type:
    """Adapt a raw JSON Schema document to the `schema_model` parameter the
    clients take: a pydantic model whose `model_json_schema()` is that document
    and whose `model_validate()` accepts any JSON object. Content validation is
    the client's structured-output binding, as with any schema_model."""
    from pydantic import RootModel

    document = copy.deepcopy(schema)

    class _JsonSchemaModel(RootModel[dict[str, Any]]):
        @classmethod
        def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return copy.deepcopy(document)

        def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return self.root

    return _JsonSchemaModel
