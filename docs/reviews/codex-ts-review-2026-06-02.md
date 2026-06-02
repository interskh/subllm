# Codex review of the TypeScript SDK plan — 2026-06-02

Reviewer: `codex exec` (gpt-5.5, read-only sandbox, web search live, isolated
CODEX_HOME), grounded against the actual local tools: **codex-cli 0.133.0**,
**Node v24.14.0**, and the live `codex exec --help`.

Verdict (original): **Executable with the fixes below.** All CRITICAL +
SHOULD-FIX findings, plus two NICE-TO-HAVE, were applied to
`docs/superpowers/plans/2026-06-02-subllm-ts-sdk.md` (and the spec where the
wording was affected) in the commit following this file.

## CRITICAL — applied

1. **`--ignore-user-config` does NOT prevent project `AGENTS.md` pollution.** It
   only skips `$CODEX_HOME/config.toml` (confirmed via `--help`); codex still
   loads `AGENTS.md` from its working directory. → `runCodexExec` now creates a
   fresh empty temp dir per call and passes `-C <dir>` (also the process `cwd`),
   plus `--ephemeral`; both are removed in `finally`. Driver + the argv test +
   `docs/drivers-contract.md` updated; the Python driver's matching gap is noted
   as a backfill TODO in the contract. *(Task 5, Task 7)*
2. **Overclaimed Gemini-style schema compatibility.** Grounded by inspecting the
   real `your-app` source: its schemas are **standard lowercase JSON Schema**
   (`object`/`string`/`array`/`integer`, `enum`/`required`/`properties`) — no
   uppercase Gemini dialect, no vendor keywords. So ajv (draft-07, `strict:false`)
   validates them directly; no normalizer needed. Spec/plan wording corrected to
   "accepts standard JSON Schema"; an uncompilable schema (e.g. uppercase
   `type:"OBJECT"`) → `ClientError`, covered by a new test. Whether codex's own
   `--output-schema` accepts a given shape is left to a maintainer live-smoke,
   not unit tests. *(Spec, Task 6)*

## SHOULD-FIX — applied

- `completeJsonSchema` made `async` so a bad schema rejects the returned promise
  instead of throwing synchronously; `JSON.stringify` wrapped → `ClientError`. *(Task 6)*
- Output-file read failure after exit 0 now maps to `OutputError` (was a raw
  Node FS error bypassing the error model). *(Task 5)*
- Added the spec-promised tests the concrete plan was missing: `CODEX_HOME`
  reaches the child env; `--` sits immediately before the prompt; `rate limit` /
  `quota` / `too many requests` all map to `QuotaError`; `CodexLLM` emits
  `model_reasoning_effort="medium"` by default. *(Task 5, Task 6)*
- Added a `file:`-consumer build smoke test (throwaway consumer installs the
  package and imports the named ESM exports) to prove the `prepare`-builds-`dist`
  chain. *(Task 8)*

## NICE-TO-HAVE — applied

- `--ephemeral` added (verified present on 0.133.0). *(Task 5)*
- `retry()` now rejects `attempts < 1` with `RangeError` instead of throwing
  `undefined`. *(Task 4)*

## NICE-TO-HAVE — not applied (tracked)

- Escaping `echoStub()` output: tests only use safe canned strings; documented as
  canned-only, not blocking.

## Validated as correct (no change needed)

- codex-cli 0.133.0 supports every planned flag: `-s read-only`,
  `--skip-git-repo-check`, `--ignore-user-config`, `-o <FILE>`, `-m <MODEL>`,
  `-c key=value`, `--output-schema <FILE>`.
- `--search` is invalid on `codex exec` (`unexpected argument '--search'`); the
  plan correctly uses `-c web_search="live"` as a single argv element.
- Node v24 `execFile`: missing binary → `code: "ENOENT"`; timeout →
  `killed: true, signal: "SIGTERM"`; PATH resolution honors the supplied `env`.
- TS `module: NodeNext` with explicit `.js` relative imports; the
  `Object.setPrototypeOf(this, new.target.prototype)` error-subclass pattern at
  `target: ES2022`; `import { setTimeout as sleep } from "node:timers/promises"`.
- Bash stubs (`#!/bin/bash`, arrays, `for ((...))`) are macOS-compatible.
- Test-count math (errors 2 → base → codexExec → codex). (Counts were since
  revised upward to 26 as the new tests above were added.)
- The Python move keeps `packages = ["src/subllm"]` correct relative to the moved
  `python/pyproject.toml`.
