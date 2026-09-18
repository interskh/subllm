"""Parse a claude session transcript into the D-4 trace, and detect the D-5 /
D-3a runtime isolation violations.

Written against committed live captures from claude 2.1.276 (see
python/tests/fixtures/claude-2.1.276-*.jsonl). The transcript — the JSONL the
client writes under `$CLAUDE_CONFIG_DIR/projects/<encoded cwd>/<session>.jsonl`
— is the trace source: a tool call is an `assistant` record holding a `tool_use`
block, and its result is the `user` record carrying `toolUseResult`.

Classification is by EXPLICIT allowlist at every layer (record type, content
block type, attachment type). Anything else that could be a tool invocation is
an isolation violation; other unrecognised, unpaired or truncated evidence marks
the trace incomplete (D-4).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from subllm.webagent import TraceEvent

#: Interactive BASH MODE tags. MEASURED in the isolated home (no model turn, so
#: no quota): a `!`-prefixed input executes a shell command locally and records
#: `<bash-input>…</bash-input>` then
#: `<bash-stdout>…</bash-stdout><bash-stderr>…</bash-stderr>` as ordinary `user`
#: string content — which the parser would otherwise read as "the prompt". Local
#: command execution is exactly D-5's case, so it is a violation here, and the
#: guarantee no longer rests on the send-time mitigation alone.
_BASH_MODE_TAGS = ("<bash-input>", "<bash-stdout>", "<bash-stderr>")

#: The tools web-agent mode may offer (D-3). Any other tool_use name — or ANY
#: tool_use at all under tools='none' (D-4c) — is a violation.
WEB_TOOLS = frozenset({"WebFetch", "WebSearch"})

#: Top-level transcript record types known NOT to be a tool invocation. Read off
#: the committed captures plus the client's own session-state records. Anything
#: else may be a tool and fails closed.
_KNOWN_RECORD_TYPES = frozenset(
    {
        "user",            # the prompt, and tool results
        "assistant",       # model output, including tool_use blocks
        "attachment",      # client-injected context; typed allowlist below
        "system",          # client notices (turn duration, errors) — no tool call
        "summary",         # conversation summary written at compaction
        "last-prompt",     # UI state: the prompt text, replayed on resume
        "mode",            # UI state: the current mode
        "permission-mode", # UI state: the permission mode
        "ai-title",        # UI state: the generated session title
        "atis-latch",      # UI state: assistant-turn-in-stream latch
        "queue-operation", # UI state: queued-prompt bookkeeping
        "cost-state",      # UI state: running cost
        # The file-backup ledger. Allowlisted ONLY while it tracks nothing: a
        # non-empty snapshot is positive evidence of a file write, and
        # `file-history-delta` is deliberately absent for the same reason
        # (mirrors the Codex parser's treatment of `turn_diff`).
        "file-history-snapshot",
        "<unparsable>",    # a line the driver could not decode; see below
    }
)

#: `assistant` content blocks that are not a tool invocation.
_KNOWN_ASSISTANT_BLOCKS = frozenset({"text", "thinking", "redacted_thinking"})
#: `user` content blocks that are not a tool invocation.
_KNOWN_USER_BLOCKS = frozenset({"text", "image", "tool_result"})

#: Attachment types that carry no tool and no isolation signal.
_KNOWN_ATTACHMENTS = frozenset(
    {
        "date", "date_change", "model", "environment", "session_context",
        "total_tokens_reminder", "prompt_snapshot", "instructions",
        "output_style", "output_style_instructions", "silent_turn_reminder",
        "batching_reminder_sent", "thinking_stripped", "auto_mode",
        "deferred_tools_record", "deferred_tools_delta", "skill_listing",
        "mcp_instructions_delta", "agent_listing_delta",
    }
)
#: D-3a, the positive-proof branch, available on 2.1.276 as a STRENGTHENING of
#: the admitted-version record (measured in all five committed captures): the
#: `prompt_snapshot` attachment written with the request carries `tools`, the
#: exact inventory the model was offered — `["WebFetch","WebSearch"]` under
#: tools='web' and `[]` under tools='none'. It is read after the turn, so it
#: cannot gate the model before it acts on untrusted content; that is what the
#: admitted-version record does. A name outside the allowed set is a violation;
#: a transcript that reports no inventory at all leaves the trace incomplete.
_INVENTORY_ATTACHMENT = "prompt_snapshot"

#: Attachments whose mere presence WITH CONTENT means settings/tool isolation
#: failed: a skill, an MCP server or a sub-agent was offered to the model
#: (D-3a's positive negative-signals).
_MUST_BE_EMPTY = ("skill_listing", "mcp_instructions_delta", "agent_listing_delta")


@dataclass
class ParsedTranscript:
    events: list[TraceEvent] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    incomplete_reasons: list[str] = field(default_factory=list)
    client_version: str = ""
    model: str = ""
    answer: str | None = None
    #: True once a prompt_snapshot reported the offered tool inventory (D-3a).
    saw_inventory: bool = False

    @property
    def trace_complete(self) -> bool:
        return not self.incomplete_reasons


def parse_transcript(
    records: Iterable[dict[str, Any]], *, tools: Literal["web", "none"] = "web"
) -> ParsedTranscript:
    out = ParsedTranscript()
    calls: dict[str, dict[str, Any]] = {}     # tool_use id -> call record
    inputs: dict[str, dict[str, Any]] = {}    # tool_use id -> tool_use block
    order: list[str] = []                     # tool_use ids, in call order
    results: dict[str, dict[str, Any]] = {}   # tool_use id -> result record
    violating: set[str] = set()

    for record in records:
        rtype = record.get("type")
        if rtype not in _KNOWN_RECORD_TYPES:
            _unknown(out, f"record type {rtype!r}", record)
            continue
        version = record.get("version")
        if version and not out.client_version:
            out.client_version = str(version)

        if rtype == "<unparsable>":
            out.incomplete_reasons.append("a transcript line could not be parsed")
            continue

        if rtype == "attachment":
            _attachment(out, record, tools=tools)
            continue

        if rtype == "file-history-snapshot":
            snapshot = record.get("snapshot")
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            if snapshot.get("trackedFileBackups"):
                out.violations.append(
                    "file-history-snapshot tracks file backups — a file was "
                    "written, which no web tool can do"
                )
                out.events.append(
                    TraceEvent(
                        kind="other", raw_type="file-history-snapshot", raw=record
                    )
                )
            continue

        if rtype == "assistant":
            _assistant(out, record, tools, calls, inputs, order, violating)
            continue

        if rtype == "user":
            _user(out, record, results)
            continue
        # The remaining allowlisted types are UI/session state: no tool call and
        # nothing the trace needs.

    for call_id in order:
        block = inputs[call_id]
        name = str(block.get("name") or "")
        result = results.pop(call_id, None)
        if call_id in violating:
            # Carry what the violating call RETURNED: that is where leaked
            # content would show, and D-4 wants the failure diagnosable from the
            # result itself.
            if result is not None:
                out.events.append(
                    TraceEvent(
                        kind="other",
                        raw_type=f"tool_result:{name}",
                        raw=result,
                    )
                )
            continue
        if result is None:
            out.incomplete_reasons.append(
                f"tool call {name!r} ({call_id}) has no matching result"
            )
        raw = {"call": calls[call_id], "result": result}
        if name == "WebSearch":
            out.events.append(_search_event(out, block, result, raw))
        else:
            out.events.append(_fetch_event(out, block, result, raw))

    for call_id, result in results.items():
        out.incomplete_reasons.append(
            f"tool result {call_id!r} has no matching call"
        )
        out.events.append(
            TraceEvent(kind="other", raw_type="tool_result/unpaired", raw=result)
        )

    if tools == "web" and not any(e.kind in ("search", "fetch") for e in out.events):
        out.incomplete_reasons.append("no web events on a web-tool call")
    if not out.saw_inventory:
        out.incomplete_reasons.append(
            "no prompt_snapshot reported the offered tool inventory"
        )
    if not out.client_version:
        out.incomplete_reasons.append("no transcript record reported a version")
    if not out.model:
        out.incomplete_reasons.append("no assistant record reported the model")
    if out.answer is None:
        out.incomplete_reasons.append("no completed assistant text in the transcript")
    return out


def _unknown(out: ParsedTranscript, what: str, record: dict[str, Any]) -> None:
    """An unrecognised record is a possible tool invocation, so it is a
    violation (D-4). The incomplete state is reserved for evidence we know is
    not a tool call — an unpaired call, an unparsable line, zero web events."""
    out.violations.append(f"unrecognised record that may be a tool: {what}")
    out.events.append(TraceEvent(kind="other", raw_type=what, raw=record))


def _assistant(
    out: ParsedTranscript,
    record: dict[str, Any],
    tools: str,
    calls: dict[str, dict[str, Any]],
    inputs: dict[str, dict[str, Any]],
    order: list[str],
    violating: set[str],
) -> None:
    message = record.get("message")
    message = message if isinstance(message, dict) else {}
    model = message.get("model")
    if model and not out.model:
        out.model = str(model)
    content = message.get("content")
    content = content if isinstance(content, list) else []
    text = ""
    for block in content:
        if not isinstance(block, dict):
            _unknown(out, "assistant content block that is not an object", record)
            continue
        btype = block.get("type")
        if btype in _KNOWN_ASSISTANT_BLOCKS:
            if btype == "text":
                text += str(block.get("text") or "")
            continue
        if btype != "tool_use":
            _unknown(out, f"assistant content block {btype!r}", record)
            continue
        name = str(block.get("name") or "")
        call_id = str(block.get("id") or "")
        calls[call_id] = record
        inputs[call_id] = block
        order.append(call_id)
        if tools == "none":
            out.violations.append(
                f"tools='none' call invoked the tool {name!r}"
            )
            violating.add(call_id)
            out.events.append(
                TraceEvent(kind="other", raw_type=f"tool_use:{name}", raw=record)
            )
            continue
        if name not in WEB_TOOLS:
            out.violations.append(f"tool {name!r} is not a web tool")
            violating.add(call_id)
            out.events.append(
                TraceEvent(kind="other", raw_type=f"tool_use:{name}", raw=record)
            )
    if text and message.get("stop_reason") == "end_turn":
        # The LAST text-bearing end_turn is the answer, exactly as the text path
        # reads it: a thinking-only end_turn is not a completed answer.
        out.answer = text


def _user(
    out: ParsedTranscript,
    record: dict[str, Any],
    results: dict[str, dict[str, Any]],
) -> None:
    message = record.get("message")
    message = message if isinstance(message, dict) else {}
    content = message.get("content")
    if isinstance(content, str):
        tag = next((t for t in _BASH_MODE_TAGS if t in content), None)
        if tag is not None:
            out.violations.append(
                f"interactive bash mode ran a local command ({tag} in a user "
                f"record)"
            )
            out.events.append(
                TraceEvent(kind="other", raw_type="bash-mode", raw=record)
            )
        return  # otherwise: the prompt
    if not isinstance(content, list):
        _unknown(out, "user record with no readable content", record)
        return
    for block in content:
        if isinstance(block, str):
            continue  # plain text, not a tool
        if not isinstance(block, dict):
            _unknown(out, "user content block that is not an object", record)
            continue
        btype = block.get("type")
        if btype not in _KNOWN_USER_BLOCKS:
            _unknown(out, f"user content block {btype!r}", record)
            continue
        if btype == "tool_result":
            results[str(block.get("tool_use_id") or "")] = record


def _attachment(
    out: ParsedTranscript, record: dict[str, Any], *, tools: str
) -> None:
    attachment = record.get("attachment")
    attachment = attachment if isinstance(attachment, dict) else {}
    atype = attachment.get("type")
    if atype not in _KNOWN_ATTACHMENTS:
        _unknown(out, f"attachment type {atype!r}", record)
        return
    if atype == _INVENTORY_ATTACHMENT:
        offered = attachment.get("tools")
        if not isinstance(offered, list):
            return  # the pre-request snapshot carries the prompt only
        out.saw_inventory = True
        allowed = WEB_TOOLS if tools == "web" else frozenset()
        names = {
            str(t.get("name")) for t in offered if isinstance(t, dict)
        } | {str(t) for t in offered if not isinstance(t, dict)}
        extra = sorted(names - set(allowed))
        if extra:
            out.violations.append(
                f"the client offered the model tool(s) {extra}, outside the "
                f"allowed set {sorted(allowed)}"
            )
            out.events.append(
                TraceEvent(kind="other", raw_type=f"attachment:{atype}", raw=record)
            )
        return
    if atype in ("deferred_tools_delta", "deferred_tools_record"):
        allowed = WEB_TOOLS if tools == "web" else frozenset()
        added = attachment.get("addedNames") or attachment.get("names") or []
        added = added if isinstance(added, list) else [added]
        extra = sorted({str(n) for n in added} - set(allowed))
        if extra:
            # D-3a: the client itself is telling us it offered the model tools
            # beyond the allowed set — tool isolation failed.
            out.violations.append(
                f"{atype} offered non-web tool(s) {extra}"
            )
            out.events.append(
                TraceEvent(kind="other", raw_type=f"attachment:{atype}", raw=record)
            )
        return
    if atype in _MUST_BE_EMPTY and _has_content(attachment):
        out.violations.append(
            f"{atype} attachment is non-empty — settings/tool isolation failed"
        )
        out.events.append(
            TraceEvent(kind="other", raw_type=f"attachment:{atype}", raw=record)
        )


def _has_content(attachment: dict[str, Any]) -> bool:
    for key, value in attachment.items():
        if key in ("type", "uuid", "timestamp", "sessionId"):
            continue
        if value:
            return True
    return False


def _search_event(
    out: ParsedTranscript,
    block: dict[str, Any],
    result: dict[str, Any] | None,
    raw: dict[str, Any],
) -> TraceEvent:
    payload = block.get("input")
    payload = payload if isinstance(payload, dict) else {}
    query = payload.get("query")
    urls: list[str] = []
    tool_result = (result or {}).get("toolUseResult")
    if isinstance(tool_result, dict):
        for item in tool_result.get("results") or []:
            if isinstance(item, str):
                continue  # the client's own prose about the search, not a result
            if not isinstance(item, dict):
                continue
            for entry in item.get("content") or []:
                if isinstance(entry, dict) and entry.get("url"):
                    url = str(entry["url"])
                    if url not in urls:
                        urls.append(url)
    elif result is not None and not isinstance(tool_result, str):
        out.incomplete_reasons.append(
            f"WebSearch result for {query!r} is not readable"
        )
    return TraceEvent(
        kind="search",
        raw_type="tool_use:WebSearch",
        raw=raw,
        query=str(query) if query is not None else None,
        result_urls=tuple(urls),
    )


def _fetch_event(
    out: ParsedTranscript,
    block: dict[str, Any],
    result: dict[str, Any] | None,
    raw: dict[str, Any],
) -> TraceEvent:
    payload = block.get("input")
    payload = payload if isinstance(payload, dict) else {}
    requested = payload.get("url")
    requested = str(requested) if requested else None
    tool_result = (result or {}).get("toolUseResult")

    ok: bool | None = None
    final: str | None = None
    if result is None:
        pass  # already marked incomplete by the caller
    elif isinstance(tool_result, str):
        # An error (the client's own refusal, or the egress filter's) arrives as
        # a plain string, not the structured record: a failed fetch, not
        # unknown evidence.
        ok = False
    elif isinstance(tool_result, dict):
        code = tool_result.get("code")
        if isinstance(code, bool) or not isinstance(code, int):
            out.incomplete_reasons.append(
                f"WebFetch result for {requested!r} carries no status code"
            )
        else:
            ok = 200 <= code < 300
            if ok:
                # D-4 / the slice ruling: on a 2xx the final URL is the one the
                # client reports (it equals the requested URL in every capture),
                # justified by this client's redirect contract — a cross-host
                # redirect comes back as a 3xx result that was NOT followed
                # (measured) — plus the egress filter having validated the
                # destination before connecting. A 3xx or error result is NOT a
                # retrieval, so it gets no final URL. Residual caveat: a
                # SAME-HOST path-level redirect can be followed silently, so the
                # final URL is exact at host granularity and may differ in path.
                reported = tool_result.get("url")
                final = str(reported) if reported else requested
    else:
        out.incomplete_reasons.append(
            f"WebFetch result for {requested!r} is not readable"
        )
    return TraceEvent(
        kind="fetch",
        raw_type="tool_use:WebFetch",
        raw=raw,
        url=requested,
        final_url=final,
        ok=ok,
    )
