"""Minimal CLI for smoke-testing both clients (and shell-out from non-Python).

    subllm complete       --client codex|claude [--model M] [--effort E] [PROMPT]
    subllm complete-json  --client codex|claude [--schema schema.json] [PROMPT]
    subllm web-agent      --client codex|claude --model M [--tools web|none]
                          [--schema schema.json] [--timeout S] [PROMPT]
    subllm image          --route codex -o OUT_DIR [PROMPT]

PROMPT is read from the argument, or stdin when omitted / '-'.

`web-agent` is the machine-facing entry point: one JSON document on stdout on
success, and a typed error JSON on stdout (never stderr) on failure. The older
subcommands keep their human-facing behaviour, errors on stderr.
"""
from __future__ import annotations

import argparse
import json
import sys

from subllm.errors import (
    ClientError,
    IsolationError,
    OutputError,
    QuotaError,
    RegionError,
    SubllmError,
)
from subllm.webagent import json_safe, schema_model_from_json_schema


def _read_prompt(arg: str | None) -> str:
    if arg and arg != "-":
        return arg
    return sys.stdin.read()


def _build_client(name: str, args: argparse.Namespace):
    if name == "codex":
        from subllm.codex import CodexLLM
        return CodexLLM(model=args.model, reasoning_effort=args.effort)
    if name == "claude":
        from subllm.claude import ClaudeLLM
        return ClaudeLLM(model=args.model)
    raise SystemExit(f"unknown client: {name!r} (expected codex|claude)")


def _build_web_agent_client(name: str, model: str | None):
    if name == "codex":
        from subllm.codex import CodexLLM
        return CodexLLM(model=model)
    if name == "claude":
        from subllm.claude import ClaudeLLM
        return ClaudeLLM(model=model)
    raise ClientError(f"unknown client: {name!r} (expected codex|claude)")


_ERROR_NAMES: tuple[tuple[type[SubllmError], str], ...] = (
    (QuotaError, "quota"),
    (IsolationError, "isolation"),
    (RegionError, "region"),
    (OutputError, "output"),
    (ClientError, "client"),
)


def _error_name(exc: SubllmError) -> str:
    for cls, name in _ERROR_NAMES:
        if isinstance(exc, cls):
            return name
    return "client"


def _json_document(payload: object) -> str:
    # json_safe also drops lone surrogates and non-finite floats, so the result
    # is strict JSON that any consumer (jq, JS, Go) accepts; allow_nan=False
    # turns a miss into a caught failure rather than a bare NaN on stdout.
    return json.dumps(json_safe(payload), ensure_ascii=False, allow_nan=False)


def _write_json_line(document: str) -> None:
    # Encode first: a write that fails half-way would leave a torn document.
    encoding = sys.stdout.encoding or "utf-8"
    try:
        document.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        # Re-encoding the same Unicode as UTF-8 does NOT help an ASCII stdout.
        # Re-dump with \uXXXX escapes instead: pure ASCII, still strict JSON
        # and still the same values after json.loads.
        try:
            document = json.dumps(
                json.loads(document), ensure_ascii=True, allow_nan=False
            )
        except Exception:  # noqa: BLE001 — not re-parseable; drop to escapes
            document = document.encode("ascii", "backslashreplace").decode("ascii")
    sys.stdout.write(document + "\n")


def _safe_str(exc: BaseException) -> str:
    try:
        return str(exc)
    except Exception:  # noqa: BLE001 — an exception whose __str__ raises
        return f"<unprintable {type(exc).__name__}>"


def _emit_web_agent_error(
    name: str, message: str, events: object = None
) -> int:
    # Serializing the trace happens HERE, under this guard: doing it in the
    # caller's except-clause would let a bad record escape uncaught (a sibling
    # except does not catch what a handler raises) and leave stdout empty.
    try:
        trace = [ev.to_json() for ev in events] if events is not None else None
        document = _json_document({"error": name, "message": message, "trace": trace})
    except Exception:  # noqa: BLE001
        try:
            document = _json_document(
                {"error": name, "message": message, "trace": None}
            )
        except Exception:  # noqa: BLE001
            document = json.dumps(
                {"error": name, "message": "unserializable error", "trace": None}
            )
    _write_json_line(document)
    return 1


def _run_web_agent(args: argparse.Namespace) -> int:
    schema_model = None
    if args.schema:
        try:
            with open(args.schema) as fh:
                document = json.load(fh)
            if not isinstance(document, dict):
                raise ValueError(
                    f"expected a JSON Schema object, got {type(document).__name__}"
                )
        except (OSError, ValueError) as e:
            # §4.2: the machine consumer never parses stderr. The call never
            # reached the model, so this is a client-side setup failure.
            # (JSONDecodeError is a ValueError.) Argparse-level errors — an
            # unknown flag — stay argparse's rc 2 on stderr.
            return _emit_web_agent_error(
                "client", f"--schema {args.schema}: {e}"
            )
        schema_model = schema_model_from_json_schema(document)

    try:
        client = _build_web_agent_client(args.client, args.model)
        run_web_task = getattr(client, "run_web_task", None)
        if run_web_task is None:
            raise ClientError(
                f"client {args.client!r} does not implement web-agent mode"
            )
        kwargs: dict[str, object] = {"tools": args.tools}
        if args.timeout is not None:
            kwargs["timeout_s"] = args.timeout
        result = run_web_task(_read_prompt(args.prompt), schema_model, **kwargs)
        document = _json_document(result.to_json())
    except SubllmError as e:
        # Non-null trace ONLY for isolation (§4.2), pinned to the type so a
        # later ClientError carrying a trace cannot widen the contract. The
        # events go in unserialized; _emit_web_agent_error degrades the trace
        # to null rather than the document if they cannot be rendered.
        events = e.trace if isinstance(e, IsolationError) else None
        return _emit_web_agent_error(_error_name(e), _safe_str(e), events)
    except Exception as e:  # noqa: BLE001
        # An internal failure (bad trace object, unserializable record) must
        # still leave one typed JSON document on stdout, never a traceback on
        # stderr: the consumer parses stdout only. The enum is closed, so an
        # unexpected failure is reported as a client failure.
        return _emit_web_agent_error("client", f"{type(e).__name__}: {_safe_str(e)}")

    _write_json_line(document)
    return 0


def _run_image(args: argparse.Namespace) -> int:
    from subllm.codex import CodexLLM

    try:
        result = CodexLLM().generate_image(_read_prompt(args.prompt), args.out_dir)
        document = _json_document({
            "paths": [str(p) for p in result.paths],
            "route": result.route,
            "text": result.text,
            "conversation_url": result.conversation_url,
            "served_model": result.served_model,
        })
    except SubllmError as e:
        sys.stderr.write(f"error: {type(e).__name__}: {_safe_str(e)}\n")
        return 1
    _write_json_line(document)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="subllm")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for cmd in ("complete", "complete-json"):
        p = sub.add_parser(cmd)
        p.add_argument("--client", required=True, choices=["codex", "claude"])
        p.add_argument("--model", default=None)
        p.add_argument("--effort", default="medium")
        p.add_argument("--schema", default=None, help="JSON-schema file (complete-json)")
        p.add_argument("prompt", nargs="?", default=None)

    w = sub.add_parser("web-agent")
    w.add_argument("--client", required=True, choices=["codex", "claude"])
    w.add_argument("--model", required=True)
    w.add_argument("--tools", default="web", choices=["web", "none"])
    w.add_argument("--schema", default=None, help="JSON-schema file")
    w.add_argument("--timeout", type=float, default=None)
    w.add_argument("prompt", nargs="?", default=None)

    i = sub.add_parser("image")
    i.add_argument("--route", required=True, choices=["codex"])
    i.add_argument("-o", "--out-dir", required=True)
    i.add_argument("prompt", nargs="?", default=None)

    try:
        args = parser.parse_args(argv)  # argparse exits(2) on bad args/choices
        if args.cmd == "web-agent":
            return _run_web_agent(args)
        if args.cmd == "image":
            return _run_image(args)
        client = _build_client(args.client, args)
        prompt = _read_prompt(args.prompt)
        if args.cmd == "complete":
            sys.stdout.write(client.complete(prompt))
        else:
            if args.schema:
                with open(args.schema) as fh:
                    schema_dict = json.loads(fh.read())
                result = client.complete_json(
                    prompt + "\n\nMatch this JSON schema: " + json.dumps(schema_dict)
                )
            else:
                result = client.complete_json(prompt)
            sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
        return 0
    except SubllmError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1
    except SystemExit as e:
        # argparse (bad args/choices) or _build_client raised SystemExit.
        return e.code if isinstance(e.code, int) else 2


if __name__ == "__main__":
    raise SystemExit(main())
