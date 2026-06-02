"""Minimal CLI for smoke-testing both clients (and shell-out from non-Python).

    subllm complete       --client codex|claude [--model M] [--effort E] [PROMPT]
    subllm complete-json  --client codex|claude [--schema schema.json] [PROMPT]

PROMPT is read from the argument, or stdin when omitted / '-'.
"""
from __future__ import annotations

import argparse
import json
import sys

from subllm.errors import SubllmError


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

    try:
        args = parser.parse_args(argv)  # argparse exits(2) on bad args/choices
        client = _build_client(args.client, args)
        prompt = _read_prompt(args.prompt)
        if args.cmd == "complete":
            sys.stdout.write(client.complete(prompt))
        else:
            if args.schema:
                schema_dict = json.loads(open(args.schema).read())
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
