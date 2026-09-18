"""Parse a codex-cli session rollout into the D-4 trace, and detect the D-5 /
D-3a runtime isolation violations.

Written against committed live captures from codex-cli 0.154.0 / gpt-5.6-luna
(see python/tests/fixtures/codex-0.154.0-*.jsonl). The rollout — not
`codex exec --json` — is the trace source: the simplified stream collapses a
two-URL page open into one item and carries no success signal.

On this client EVERY tool invocation is a `custom_tool_call` named `exec` whose
input is JavaScript running in code mode, so the isolation check is a scan of
that script plus a classification of every other record in the stream.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from subllm.webagent import TraceEvent

# --- record classification -------------------------------------------------

# Classification is by EXPLICIT allowlist in every layer: only these record
# kinds are known not to be a tool invocation. Anything else is an isolation
# violation (D-4: "an unrecognised record that is, or may be, a tool invocation
# is an isolation violation"). A name-pattern heuristic was rejected — it routed
# codex's own `FileChange` and `Extension`/`apps` items to "incomplete".
_KNOWN_RECORD_TYPES = frozenset(
    {"session_meta", "turn_context", "world_state", "token_usage_record",
     "response_item", "event_msg", "compacted"}
)
_KNOWN_RESPONSE_ITEMS = frozenset(
    {"message", "reasoning", "custom_tool_call", "custom_tool_call_output"}
)
_KNOWN_EVENTS = frozenset(
    {"task_started", "task_complete", "token_count", "item_completed",
     "item_started", "item_updated", "turn_diff", "error"}
)
# item_completed item types that cannot be a tool invocation. `Extension` is
# handled separately: it carries the web items, and any other extension kind is
# treated as a possible tool.
_KNOWN_ITEM_TYPES = frozenset({"UserMessage", "Reasoning", "AgentMessage"})

_SEARCH_ACTIONS = frozenset({"search"})
_OPEN_ACTIONS = frozenset({"openPage", "open"})
_FIND_ACTIONS = frozenset({"find_in_page", "findInPage", "find"})

# --- script scanning (D-5) -------------------------------------------------

# The only tool binding web-agent mode allows. Matching `\btools\b` and then
# requiring `.web__run` immediately after it also catches `tools["apply_patch"]`,
# `const {apply_patch} = tools` and `tools?.x`, which a `tools\.(\w+)` regex
# would miss.
_TOOLS_REF = re.compile(r"\btools\b")
_ALLOWED_BINDING = re.compile(r"^\.web__run\b")

# Identifiers that would mean the script is reaching outside the tool bindings.
_ESCAPES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern))
    for name, pattern in (
        ("ALL_TOOLS", r"\bALL_TOOLS\b"),
        ("fetch(", r"\bfetch\s*\("),
        ("process", r"\bprocess\b"),
        ("Deno", r"\bDeno\b"),
        ("require(", r"\brequire\s*\("),
        ("import(", r"\bimport\s*\("),
        ("eval", r"\beval\b"),
        ("Function", r"\bFunction\b"),
        ("globalThis", r"\bglobalThis\b"),
    )
)

# ECMAScript allows \uXXXX / \u{…} / \xXX escapes INSIDE identifiers, so
# `tools.\u0061pply_patch(…)` runs the real call while spelling nothing we match.
# A legitimate `tools.web__run` call never needs an escape sequence anywhere, so
# any escape in the script is a violation rather than a parsing problem.
_IDENT_ESCAPE = re.compile(r"\\u\{[0-9a-fA-F]+\}|\\u[0-9a-fA-F]{0,4}|\\x[0-9a-fA-F]{0,2}")

_OPEN_KEY = re.compile(r"\bopen\s*:\s*")
_REF_ID = re.compile(r"\bref_id\s*:\s*(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')")
_REF_ID_KEY = re.compile(r"\bref_id\s*:")


def scan_script(script: str) -> list[str]:
    """Isolation violations in one code-mode script."""
    problems: list[str] = []
    if _IDENT_ESCAPE.search(script):
        problems.append(
            "script used a \\u/\\x escape sequence, which can spell an "
            "identifier the scan would not match"
        )
    for refs, determinate in open_ref_arrays(script):
        if not determinate:
            # D-4b needs the requested targets. If they are computed rather than
            # written out, a non-public target cannot be ruled out.
            problems.append(
                "script used an open: call whose ref_id targets are not "
                "statically determinable"
            )
    for match in _TOOLS_REF.finditer(script):
        rest = script[match.end():]
        if not _ALLOWED_BINDING.match(rest):
            used = re.match(r"[.\[]?\s*[\"']?([A-Za-z0-9_$]+)", rest)
            name = used.group(1) if used else "<indirect>"
            problems.append(f"script used tool binding tools.{name}")
    for name, pattern in _ESCAPES:
        if pattern.search(script):
            problems.append(f"script referenced runtime-escape identifier {name}")
    return problems


def open_ref_arrays(script: str) -> list[tuple[list[str], bool]]:
    """The `open:[{ref_id:"…"}]` arrays of a script, in order, each as its list
    of requested URLs plus whether that list is the WHOLE array. These — never
    `action.url`, which is only the first ref_id of a multi-open call — are the
    requested fetch targets. A computed ref_id (`"file:"+"///x"`, a `.map(…)`)
    yields determinate=False: the targets cannot be checked."""
    arrays: list[tuple[list[str], bool]] = []
    for match in _OPEN_KEY.finditer(script):
        if match.end() >= len(script) or script[match.end()] != "[":
            # `open: urls.map(…)` — not even an array literal, so nothing about
            # the requested targets can be read from the script.
            arrays.append(([], False))
            continue
        start = match.end() + 1  # just past '['
        depth = 1
        i = start
        while i < len(script) and depth:
            ch = script[i]
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
            elif ch in "\"'":
                quote = ch
                i += 1
                while i < len(script) and script[i] != quote:
                    i += 2 if script[i] == "\\" else 1
            i += 1
        body = script[start:i - 1]
        literals = [json.loads(_to_json_string(m.group(1)))
                    for m in _REF_ID.finditer(body)]
        keys = len(_REF_ID_KEY.findall(body))
        determinate = keys == len(literals) and (keys > 0 or not body.strip())
        arrays.append((literals, determinate))
    return arrays


def _to_json_string(literal: str) -> str:
    if literal.startswith("'"):
        return '"' + literal[1:-1].replace('"', '\\"').replace("\\'", "'") + '"'
    return literal


# --- parse result ----------------------------------------------------------


@dataclass
class ParsedRollout:
    events: list[TraceEvent] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    incomplete_reasons: list[str] = field(default_factory=list)
    client_version: str = ""

    @property
    def trace_complete(self) -> bool:
        return not self.incomplete_reasons


def parse_rollout(
    records: Iterable[dict[str, Any]], *, tools: Literal["web", "none"] = "web"
) -> ParsedRollout:
    out = ParsedRollout()
    pending_opens: list[list[str]] = []
    open_calls: dict[str, dict[str, Any]] = {}
    closed_calls: set[str] = set()
    saw_task_complete = False
    web_items = 0

    for record in records:
        rtype = record.get("type")
        payload = record.get("payload")
        payload = payload if isinstance(payload, dict) else {}

        if rtype not in _KNOWN_RECORD_TYPES:
            _unknown(out, f"record type {rtype!r}", record)
            continue

        if rtype == "session_meta":
            out.client_version = str(payload.get("cli_version") or "")
            if not out.client_version:
                out.incomplete_reasons.append("session_meta carries no cli_version")
            continue

        if rtype == "response_item":
            ptype = payload.get("type")
            if ptype not in _KNOWN_RESPONSE_ITEMS:
                _unknown(out, f"response_item type {ptype!r}", record)
                continue
            if ptype == "custom_tool_call":
                name = payload.get("name")
                call_id = str(payload.get("call_id") or payload.get("id") or "")
                if name != "exec":
                    out.violations.append(f"tool call {name!r} is not code-mode exec")
                    out.events.append(_other(f"custom_tool_call:{name}", record))
                    continue
                if tools == "none":
                    out.violations.append(
                        "tools='none' call made a tool invocation (custom_tool_call)"
                    )
                    out.events.append(_other("custom_tool_call:exec", record))
                script = str(payload.get("input") or "")
                problems = scan_script(script)
                if problems:
                    out.violations.extend(problems)
                    out.events.append(_other("custom_tool_call:exec", record))
                # Determinacy is already a scan_script violation; the refs are
                # kept so the fetch events still name what was requested.
                pending_opens.extend(refs for refs, _ in open_ref_arrays(script))
                open_calls[call_id] = record
            elif ptype == "custom_tool_call_output":
                call_id = str(payload.get("call_id") or "")
                if call_id in open_calls:
                    open_calls.pop(call_id)
                    closed_calls.add(call_id)
                else:
                    out.incomplete_reasons.append(
                        f"tool output {call_id!r} has no matching call"
                    )
            continue

        if rtype == "event_msg":
            ptype = payload.get("type")
            if ptype not in _KNOWN_EVENTS:
                _unknown(out, f"event_msg type {ptype!r}", record)
                continue
            if ptype == "task_complete":
                saw_task_complete = True
                continue
            if ptype != "item_completed":
                continue
            item = payload.get("item")
            item = item if isinstance(item, dict) else {}
            itype = item.get("type")
            if itype in _KNOWN_ITEM_TYPES:
                continue
            if itype != "Extension" or item.get("kind") != "web.search":
                _unknown(out, f"item {itype!r}/{item.get('kind')!r}", record)
                continue
            web_items += 1
            if tools == "none":
                out.violations.append("tools='none' call produced a web tool event")
                out.events.append(_other("web.search", record))
                continue
            _web_item(out, item, record, pending_opens)
            continue

        # turn_context / world_state / token_usage_record / compacted carry no
        # tool invocation and nothing the trace needs.

    if open_calls:
        out.incomplete_reasons.append(
            f"{len(open_calls)} tool call(s) without a paired output"
        )
    if not saw_task_complete:
        out.incomplete_reasons.append("stream ended without task_complete")
    if tools == "web" and web_items == 0:
        out.incomplete_reasons.append("no web events on a web-tool call")
    return out


def _unknown(out: ParsedRollout, what: str, record: dict[str, Any]) -> None:
    """An unrecognised record is a possible tool invocation, so it is a
    violation (D-4). Only records on the explicit non-tool allowlists above
    reach the ordinary paths; the incomplete state is for evidence we know is
    not a tool call — an unpaired call, a truncated stream, an unknown web
    action, zero web events."""
    out.violations.append(f"unrecognised record that may be a tool: {what}")
    out.events.append(_other(what, record))


def _other(raw_type: str, record: dict[str, Any]) -> TraceEvent:
    return TraceEvent(kind="other", raw_type=raw_type, raw=record)


def _web_item(
    out: ParsedRollout,
    item: dict[str, Any],
    record: dict[str, Any],
    pending_opens: list[list[str]],
) -> None:
    action = item.get("action")
    action = action if isinstance(action, dict) else {}
    atype = action.get("type")
    results = item.get("results")
    results = results if isinstance(results, list) else []

    if atype in _SEARCH_ACTIONS:
        urls = tuple(
            str(r.get("url")) for r in results
            if isinstance(r, dict) and r.get("url")
        )
        out.events.append(
            TraceEvent(
                kind="search",
                raw_type="web.search/search",
                raw=record,
                query=action.get("query") or item.get("query"),
                result_urls=urls,
            )
        )
        return

    if atype in _OPEN_ACTIONS or atype in _FIND_ACTIONS:
        requested: list[str] = []
        if atype in _OPEN_ACTIONS:
            requested = pending_opens.pop(0) if pending_opens else []
            if len(requested) != len(results):
                out.incomplete_reasons.append(
                    f"page open requested {len(requested)} url(s) but the item "
                    f"reports {len(results)} result(s)"
                )
        for i in range(max(len(requested), len(results))):
            url = requested[i] if i < len(requested) else None
            result = results[i] if i < len(results) else None
            result = result if isinstance(result, dict) else {}
            final = result.get("url")
            out.events.append(
                TraceEvent(
                    kind="fetch",
                    raw_type=f"web.search/{atype}",
                    raw=record,
                    url=url,
                    final_url=str(final) if final else None,
                    ok=bool(final),
                )
            )
        return

    out.incomplete_reasons.append(f"unknown web action type {atype!r}")
    out.events.append(_other(f"web.search/{atype}", record))
