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
    # `turn_diff` is deliberately absent: it is positive evidence of a file
    # write, so it takes the violation path like any other tool evidence.
    {"task_started", "task_complete", "token_count", "item_completed",
     "item_started", "item_updated", "error"}
)
# item_completed item types that cannot be a tool invocation. `Extension` is
# handled separately: it carries the web items, and any other extension kind is
# treated as a possible tool.
_KNOWN_ITEM_TYPES = frozenset({"UserMessage", "Reasoning", "AgentMessage"})

_URL_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")

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

# The scan is an ALLOWLIST of the SHAPE the real client emits, not a blacklist
# of bad spellings: a blacklist is bypassable through the prototype chain
# (`tools.web__run.constructor("return tools")().apply_patch(…)` names nothing
# forbidden). It is deliberately wider than the captures, though: reading
# properties of the value `tools.web__run` RETURNED is not an escalation — that
# value is untrusted web content the script can only hand to `text()` — and a
# scan that only admitted `text(r)` would fail ordinary result handling.
_ALLOWED_GLOBALS = frozenset({"tools", "text", "JSON"})
_ALLOWED_KEYWORDS = frozenset(
    {"const", "let", "var", "await", "async", "return", "if", "else", "for",
     "of", "in", "try", "catch", "finally", "true", "false", "null",
     # `function` is deliberately absent: no legitimate client script needs a
     # function expression, and it is the cheapest constructor-bearing value.
     "undefined", "typeof", "void", "break", "continue", "while",
     "do", "switch", "case", "default", "throw"}
)
# Names that reach the prototype chain or re-bind a function's `this`. They are
# a violation in EVERY position — property, declared name, destructuring key or
# target, string literal, computed key, template expression — and are scanned on
# the RAW script, before strings and comments are blanked. A property-position
# rule alone was laundered three ways: `const {constructor: C} = async
# function(){}` never takes the property path, and `"constructor"` passed as a
# string to getOwnPropertyDescriptor was blanked before the scan ever saw it.
# No legitimate client script contains any of these names anywhere.
_DENIED_PROPERTIES = frozenset(
    {"constructor", "prototype", "__proto__", "call", "apply", "bind",
     "caller", "arguments", "__defineGetter__", "__defineSetter__",
     "__lookupGetter__", "__lookupSetter__"}
)

_DENIED_ANYWHERE = re.compile(
    r"\b(?:" + "|".join(
        ("constructor", "prototype", "__proto__", "call", "apply", "bind",
         "caller", "arguments", "__defineGetter__", "__defineSetter__",
         "__lookupGetter__", "__lookupSetter__")
    ) + r")\b"
)

# Object keys the client actually emits (read from the committed captures;
# `find` is the find-action counterpart of `open`). A generic "anything in key
# position is exempt" rule laundered a ternary's middle operand — `1 ? Object :
# 0` reads as key shape — so key position is an allowlist too.
_ALLOWED_KEYS = frozenset(
    {"open", "find", "ref_id", "search_query", "q", "response_length"}
)

_TOKEN = re.compile(r"(\.\s*)?([A-Za-z_$][A-Za-z0-9_$]*)\s*(:)?")
# Two string literals joined by `+`: the value cannot be read statically, which
# is how `C("return to"+"ols")` hid its payload.
_CONCAT_LITERALS = re.compile(r'""\s*\+\s*""')
# A computed key in an object literal or destructuring pattern (`{["x"]: v}`),
# which the member-access pattern does not cover.
_COMPUTED_KEY = re.compile(r"[{,]\s*\[")
# `[` used as member access (after an identifier, `)`, `]` or a string) rather
# than as an array literal. A NUMERIC index is allowed — it cannot spell a
# property name — but `a[b]` and `a["cons"+"tructor"]` cannot be read, so they
# are violations.
_COMPUTED_ACCESS = re.compile(r"[A-Za-z0-9_$)\]]\s*\[\s*([^\]]*)\]")
_NUMERIC_INDEX = re.compile(r"\d+")
# `tools` is usable ONLY as a call of its one allowed binding: not
# `tools.apply_patch`, not `tools.web__run.constructor`, not a bare
# `const f = tools.web__run` that could be re-bound later.
_TOOLS_CALL = re.compile(r"^\.web__run\s*\(")

_REF_ARRAY_KEY = re.compile(r"\b(?:open|find)\s*:\s*")
_REF_ID_KEY = re.compile(r"\bref_id\s*:")
# A ref_id value plus the character that follows it: anything other than `,`/`}`
# (a `+`, a call, an identifier, a template literal) means the value is
# computed, not one complete literal.
_REF_ID_VALUE = re.compile(
    r"\bref_id\s*:\s*(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')?\s*(.)"
)

_DECL = re.compile(
    r"\b(?:const|let|var)\s+(\{[^}]*\}|\[[^\]]*\]|[A-Za-z_$][A-Za-z0-9_$]*)"
)
_ARROW_PARAMS = re.compile(
    r"(?:\(([^)]*)\)|([A-Za-z_$][A-Za-z0-9_$]*))\s*=>"
)
_CATCH_PARAM = re.compile(r"\bcatch\s*\(\s*([A-Za-z_$][A-Za-z0-9_$]*)")
_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_TRAILING_KEY = re.compile(r"([A-Za-z_$][A-Za-z0-9_$]*)[\"']?$")


def _mask_data_strings(script: str) -> str:
    """Blank out the string literals that are the VALUE of an allowlisted key
    (`ref_id:"…"`, `q:"…"`, `response_length:"…"`), leaving every other string
    literal in place for the denied-name scan.

    Those values are DATA, not code: the ref_id rule already requires each to be
    one complete literal, and each is checked downstream as a fetch or search
    target. Scanning them for denied names made an ordinary URL —
    `…/Function/prototype/call`, `/docs/how-to-call-the-api`, `/apply` — raise a
    false isolation alarm, and a control that cries wolf on a normal URL gets
    turned off. A string in any OTHER position (a call argument, for example
    `getOwnPropertyDescriptor(…, "constructor")`) is still scanned."""
    out = list(script)
    i, n = 0, len(script)
    while i < n:
        ch = script[i]
        if ch in "\"'":
            start = i
            i += 1
            while i < n and script[i] != ch:
                i += 2 if script[i] == "\\" else 1
            i += 1
            head = script[:start].rstrip()
            if head.endswith(":"):
                key = _TRAILING_KEY.search(head[:-1].rstrip())
                if key is not None and key.group(1) in _ALLOWED_KEYS:
                    for j in range(start, min(i, n)):
                        out[j] = " "
        elif script.startswith("//", i):
            while i < n and script[i] != "\n":
                i += 1
        elif script.startswith("/*", i):
            end = script.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            i += 1
    return "".join(out)


def _blank_strings(script: str) -> str:
    """Drop comments and the TEXT of string literals, keeping the structure the
    token scan reads. Strings and comments are handled in ONE pass: a regex pass
    over comments first would read the `//` of `"file:///tmp/x"` as a line
    comment and desynchronise every quote after it. A template literal's
    `${…}` expressions are KEPT — they are code, and blanking them would hide
    `${r.constructor}` from the scan."""
    out: list[str] = []
    i, n = 0, len(script)
    while i < n:
        ch = script[i]
        if ch in "\"'":
            quote = ch
            i += 1
            while i < n and script[i] != quote:
                i += 2 if script[i] == "\\" else 1
            i += 1
            out.append('""')
        elif ch == "`":
            i += 1
            while i < n and script[i] != "`":
                if script[i] == "\\":
                    i += 2
                    continue
                if script.startswith("${", i):
                    depth = 1
                    i += 2
                    start = i
                    while i < n and depth:
                        if script[i] == "{":
                            depth += 1
                        elif script[i] == "}":
                            depth -= 1
                            if not depth:
                                break
                        i += 1
                    out.append("(" + _blank_strings(script[start:i]) + ")")
                    i += 1
                    continue
                i += 1
            i += 1
            out.append('""')
        elif script.startswith("//", i):
            while i < n and script[i] != "\n":
                i += 1
        elif script.startswith("/*", i):
            end = script.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _declared_names(source: str) -> set[str]:
    """Every name the script BINDS: declarations (including destructuring),
    arrow and function parameters, for-of / for-in bindings and catch params."""
    names: set[str] = set()
    for match in _DECL.finditer(source):
        group = match.group(1)
        if group.startswith(("{", "[")):
            names.update(_IDENT.findall(group))
        else:
            names.add(group)
    for match in _ARROW_PARAMS.finditer(source):
        group = next((g for g in match.groups() if g), "")
        names.update(_IDENT.findall(group))
    for match in _CATCH_PARAM.finditer(source):
        names.add(match.group(1))
    return names


def scan_script(script: str) -> list[str]:
    """Isolation violations in one code-mode script."""
    problems: list[str] = []
    # Every name-based rule reads the script with the allowlisted keys' own
    # string VALUES blanked: those are data (a URL, a query), and scanning them
    # made `…/Function/prototype/call` or `/tools/` raise a false alarm. Strings
    # in any other position stay scanned.
    masked = _mask_data_strings(script)
    for match in _DENIED_ANYWHERE.finditer(masked):
        problems.append(
            f"script used the denied name {match.group(0)!r} "
            f"(denied in every position)"
        )
    if _IDENT_ESCAPE.search(masked):
        problems.append(
            "script used a \\u/\\x escape sequence, which can spell an "
            "identifier the scan would not match"
        )
    for _key, refs, determinate in ref_arrays(script):
        if not determinate:
            # D-4b needs the requested targets. If they are computed rather than
            # written out, a non-public target cannot be ruled out.
            problems.append(
                "script used an open:/find: call whose ref_id targets are not "
                "statically determinable"
            )
    for match in _TOOLS_REF.finditer(masked):
        rest = masked[match.end():]
        if not _TOOLS_CALL.match(rest):
            used = re.match(r"[.\[]?\s*[\"']?([A-Za-z0-9_$]+)", rest)
            name = used.group(1) if used else "<indirect>"
            problems.append(f"script used tool binding tools.{name}")
    for name, pattern in _ESCAPES:
        if pattern.search(masked):
            problems.append(f"script referenced runtime-escape identifier {name}")

    source = _blank_strings(script)
    if _CONCAT_LITERALS.search(source):
        problems.append("script concatenated string literals")
    if _COMPUTED_KEY.search(source):
        problems.append("script used a computed property key")
    for match in _COMPUTED_ACCESS.finditer(source):
        key = match.group(1).strip()
        if not _NUMERIC_INDEX.fullmatch(key):
            # A numeric index cannot spell a property name; anything else can.
            problems.append(f"script used computed member access [{key}]")
    declared = _declared_names(source)
    for match in _TOKEN.finditer(source):
        is_property, name, is_key = match.group(1), match.group(2), match.group(3)
        if is_property:
            if name in _DENIED_PROPERTIES:
                problems.append(f"script read property .{name}")
        elif (
            (is_key and name in _ALLOWED_KEYS)
            or name in _ALLOWED_KEYWORDS
            or name in declared
        ):
            continue  # a known object key, a keyword, or a local binding
        elif name not in _ALLOWED_GLOBALS:
            problems.append(f"script referenced identifier {name}")
    return problems


def ref_arrays(script: str) -> list[tuple[str, list[str], bool]]:
    """The `open:[{ref_id:"…"}]` / `find:[{ref_id:"…"}]` arrays of a script, in
    order, each as its list of requested targets plus whether that list is the
    WHOLE array. These — never `action.url`, which is only the first ref_id of a
    multi-open call — are the requested fetch targets. A computed ref_id
    (`"file:"+"///x"`, `"https://ok/"+"@127.0.0.1/"`, a `.map(…)`) yields
    determinate=False: the targets cannot be checked."""
    arrays: list[tuple[str, list[str], bool]] = []
    for match in _REF_ARRAY_KEY.finditer(script):
        key = "find" if match.group(0).lstrip().startswith("find") else "open"
        if match.end() >= len(script) or script[match.end()] != "[":
            # `open: urls.map(…)` — not even an array literal, so nothing about
            # the requested targets can be read from the script.
            arrays.append((key, [], False))
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
        literals: list[str] = []
        whole = 0
        for value in _REF_ID_VALUE.finditer(body):
            literal, tail = value.group(1), value.group(2)
            # The value must be ONE complete literal: `"https://ok"+"@127.0.0.1/"`
            # reads as its public prefix alone, which is exactly the evasion.
            if literal and tail in (",", "}"):
                literals.append(json.loads(_to_json_string(literal)))
                whole += 1
        keys = len(_REF_ID_KEY.findall(body))
        determinate = keys == whole and (keys > 0 or not body.strip())
        arrays.append((key, literals, determinate))
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
    model: str = ""

    @property
    def trace_complete(self) -> bool:
        return not self.incomplete_reasons


def parse_rollout(
    records: Iterable[dict[str, Any]], *, tools: Literal["web", "none"] = "web"
) -> ParsedRollout:
    out = ParsedRollout()
    pending_opens: list[list[str]] = []
    pending_finds: list[list[str]] = []
    ref_urls: dict[str, str] = {}
    open_calls: dict[str, dict[str, Any]] = {}
    closed_calls: set[str] = set()
    violating_calls: set[str] = set()
    saw_task_complete = False
    web_items = 0

    for record in records:
        rtype = record.get("type")
        payload = record.get("payload")
        payload = payload if isinstance(payload, dict) else {}

        if rtype not in _KNOWN_RECORD_TYPES:
            _unknown(out, f"record type {rtype!r}", record)
            continue

        if rtype == "turn_context":
            model = payload.get("model")
            if model:
                out.model = str(model)
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
                    violating_calls.add(call_id)
                    open_calls[call_id] = record
                    continue
                if tools == "none":
                    out.violations.append(
                        "tools='none' call made a tool invocation (custom_tool_call)"
                    )
                    out.events.append(_other("custom_tool_call:exec", record))
                    violating_calls.add(call_id)
                script = str(payload.get("input") or "")
                problems = scan_script(script)
                if problems:
                    out.violations.extend(problems)
                    out.events.append(_other("custom_tool_call:exec", record))
                    violating_calls.add(call_id)
                # Determinacy is already a scan_script violation; the refs are
                # kept so the fetch events still name what was requested.
                for key, refs, _ in ref_arrays(script):
                    if key == "find":
                        pending_finds.append(refs)
                    else:
                        pending_opens.append(refs)
                open_calls[call_id] = record
            elif ptype == "custom_tool_call_output":
                call_id = str(payload.get("call_id") or "")
                if call_id in open_calls:
                    open_calls.pop(call_id)
                    closed_calls.add(call_id)
                    if call_id in violating_calls:
                        # Carry what the violating call RETURNED: that is where
                        # leaked content would show, and D-4 wants the failure
                        # diagnosable from the result itself.
                        out.events.append(
                            _other("custom_tool_call_output", record)
                        )
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
            _web_item(out, item, record, pending_opens, pending_finds, ref_urls)
            continue

        # turn_context / world_state / token_usage_record / compacted carry no
        # tool invocation and nothing the trace needs.

    for refs in pending_opens + pending_finds:
        # An open: request with no matching web item. Its targets were asked
        # for, so they go through the D-4b check rather than being dropped.
        for url in refs:
            out.incomplete_reasons.append(
                f"page open of {url!r} has no matching web item"
            )
            out.events.append(
                TraceEvent(
                    kind="fetch",
                    raw_type="web.search/unreported-open",
                    raw={"requested": url},
                    url=url,
                    final_url=None,
                    ok=None,
                )
            )
    if open_calls:
        out.incomplete_reasons.append(
            f"{len(open_calls)} tool call(s) without a paired output"
        )
    if not saw_task_complete:
        out.incomplete_reasons.append("stream ended without task_complete")
    if tools == "web" and web_items == 0:
        out.incomplete_reasons.append("no web events on a web-tool call")
    if not out.model:
        out.incomplete_reasons.append("no turn_context reported the model")
    return out


def _unknown(out: ParsedRollout, what: str, record: dict[str, Any]) -> None:
    """An unrecognised record is a possible tool invocation, so it is a
    violation (D-4). Only records on the explicit non-tool allowlists above
    reach the ordinary paths; the incomplete state is for evidence we know is
    not a tool call — an unpaired call, a truncated stream, an unknown web
    action, zero web events."""
    out.violations.append(f"unrecognised record that may be a tool: {what}")
    out.events.append(_other(what, record))


def _remember_refs(results: list[Any], ref_urls: dict[str, str]) -> None:
    """Handles the model can open later (`turn0search0` -> its URL)."""
    for result in results:
        if not isinstance(result, dict):
            continue
        ref_id, url = result.get("ref_id"), result.get("url")
        if ref_id and url:
            ref_urls.setdefault(str(ref_id), str(url))


def _other(raw_type: str, record: dict[str, Any]) -> TraceEvent:
    return TraceEvent(kind="other", raw_type=raw_type, raw=record)


def _resolve_ref(
    raw: str | None, ref_urls: dict[str, str]
) -> str | None:
    """A ref_id is either a URL or a handle on an earlier result
    (`turn0search0`, `turn0view1`). A handle is resolved against the results the
    trace has already seen, so the ordinary search-then-open flow works and the
    resolved URL — not the handle — is what D-4b checks."""
    if raw is None:
        return None
    if _URL_SCHEME.match(raw):
        return raw
    return ref_urls.get(raw)


def _web_item(
    out: ParsedRollout,
    item: dict[str, Any],
    record: dict[str, Any],
    pending_opens: list[list[str]],
    pending_finds: list[list[str]],
    ref_urls: dict[str, str],
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
        _remember_refs(results, ref_urls)
        return

    if atype in _OPEN_ACTIONS or atype in _FIND_ACTIONS:
        queue = pending_opens if atype in _OPEN_ACTIONS else pending_finds
        requested = queue.pop(0) if queue else []
        if len(requested) != len(results):
            out.incomplete_reasons.append(
                f"{atype} requested {len(requested)} target(s) but the item "
                f"reports {len(results)} result(s)"
            )
        for i in range(max(len(requested), len(results))):
            raw_ref = requested[i] if i < len(requested) else None
            url = _resolve_ref(raw_ref, ref_urls)
            result = results[i] if i < len(results) else None
            result = result if isinstance(result, dict) else {}
            final = result.get("url")
            if url is None:
                # A fetch-class event whose REQUESTED target cannot be
                # determined — an unresolvable ref_id, or none at all — cannot
                # be checked against D-4b, so it fails closed rather than
                # passing as an unknown address. A reported final URL does not
                # rescue it: that is the address the client ended at, not the
                # one it was asked for.
                out.violations.append(
                    f"{atype} whose fetch target could not be determined "
                    f"(ref_id {raw_ref!r})"
                )
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
        _remember_refs(results, ref_urls)
        return

    out.incomplete_reasons.append(f"unknown web action type {atype!r}")
    out.events.append(_other(f"web.search/{atype}", record))
