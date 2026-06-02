# subllm TypeScript SDK — Design Spec (2026-06-02)

## Purpose

A TypeScript/Node SDK that gives a TS worker the same `codex exec` capabilities
subllm's Python `CodexLLM` already has. Built for the `your-app` project (a
Next.js/TS consumer) so it can move its LLM scoring/verdict pipeline off
per-token Gemini/Claude APIs and onto flat-rate ChatGPT-subscription quota via
`codex exec`.

The blocker this solves: `your-app` is TypeScript and runs its pipeline as a
Node worker; it can't call subllm's Python library in-process, and the Python
**CLI** doesn't expose web search or native structured output. This SDK ports
the Python `CodexLLM` contract to idiomatic TS.

**Hard constraint (non-negotiable):** client-native execution only. NEVER build
or use a subscription-to-API HTTP proxy (a token wrapped behind an
OpenAI-compatible endpoint) — ban risk. The SDK only ever drives the real
`codex exec` CLI as a subprocess, identical to the Python side. See
`docs/drivers-contract.md` (the `codex_exec` section) for the exact contract
both languages share.

## Background facts that shape the design

- **The contract is already specified.** `docs/drivers-contract.md` documents the
  exact argv, the `web_search="live"` config override (`--search` is NOT a valid
  `codex exec` flag), output-file reading, and the quota-vs-client error
  classification. The Python implementation (`src/.../codex.py`,
  `drivers/codex_exec.py`, `base.py`, `errors.py`) is the reference to port.
- **your-app already holds its schemas as plain JSON Schema objects**
  (`Record<string, unknown>`), passed directly to Gemini's `responseSchema`. It
  does NOT use zod. Therefore `completeJsonSchema` accepts a plain JSON Schema
  object and validates the result against it — the TS analog of Pydantic's
  `model_validate`. This is why the validator is **ajv** (validates data against
  arbitrary JSON Schema), not zod (which validates against zod schemas).
  Verified against the real your-app source: its schemas use **lowercase
  standard JSON Schema** types (`object`/`string`/`array`/`integer` with
  `enum`/`required`/`properties`) — no uppercase Gemini/OpenAPI dialect, no
  vendor-only keywords. So ajv (draft-07) compiles and validates them directly;
  no schema normalization is needed.
- **your-app will pass model `gpt-5.4-mini`.** The model string is forwarded
  verbatim to `codex exec -m` — never normalized, "corrected," or downgraded.
- **Phase 1 is `CodexLLM` only.** Both your-app calls (classify-with-search and
  write-verdict-no-search) go through codex. `ClaudeLLM`/tmux, `FallbackLLM`,
  `RegionGuard`, and a CLI are out of scope for phase 1.

## Decisions (locked during brainstorming)

| Decision | Choice |
|---|---|
| Repo layout | **Polyglot, peers under root**: existing Python moves to `python/`, new TS SDK at `ts/`. `docs/` + `README.md` stay shared at root. |
| Scope (phase 1) | **`CodexLLM` + `DryRunLLM` + errors + retry + codex driver.** No Claude/tmux, no Fallback, no RegionGuard, no CLI. |
| Module system | **ESM-only**, `"type": "module"`, `engines.node >= 20`. No browser target, no CJS. |
| Build | **`tsc`** → `dist/` (`.js` + `.d.ts`), `exports` map. No bundler. |
| Test runner | **vitest** (universal across sibling TS projects). |
| Validator | **ajv** (only runtime dep) — caller supplies raw JSON Schema; ajv enforces it. |
| Package name | **`subllm`** → consumers `import { CodexLLM, QuotaError } from "subllm"`. |
| Subprocess | `node:child_process.execFile` — argv array, **no shell** (no injection). |

## Repo restructure (step 0)

```
subllm/
  python/                  # git mv of existing tree: src/, tests/, pyproject.toml, uv.lock, .gitignore-as-needed
  ts/                      # NEW — this SDK
  docs/                    # shared (drivers-contract.md is the cross-language source of truth)
  README.md                # stays at root; updated to document both python/ and ts/
  AGENTS.md / CLAUDE.md     # repo-level handoff, stay at root
```

Consequence: the Python consumer path changes from `…/subllm` →
`…/subllm/python`. The README install instructions and any path references in
`AGENTS.md` are updated accordingly. Python tests must still pass from the new
`python/` location.

## TS package layout (`ts/`)

```
ts/
  package.json             # name "subllm", type module, exports map, engines node>=20
  tsconfig.json            # target ES2022, module/moduleResolution NodeNext, strict
  vitest.config.ts         # (or config in package.json)
  src/
    index.ts               # public exports
    base.ts                # BaseLLM type/abstract + retry() + DryRunLLM + validateWithSchema()
    errors.ts              # SubllmError, QuotaError, ClientError, OutputError
    codex.ts               # CodexLLM
    drivers/
      codexExec.ts         # runCodexExec() — the subprocess contract (TS port of codex_exec.py)
  test/
    bin/                   # stub `codex` script(s) used by tests
    *.test.ts
  dist/                    # tsc output (gitignored)
```

The `drivers/codexExec.ts` file is the isolated subprocess boundary — the TS
realization of the same `codex_exec` contract the Python driver implements.

## Public API (mirrors Python, idiomatic TS)

```ts
export interface CodexOptions {
  model?: string;            // forwarded verbatim to `codex exec -m`
  reasoningEffort?: string;  // default "medium"
  search?: boolean;          // default false; true => -c web_search="live"
  codexHome?: string;        // CODEX_HOME isolation dir
  attempts?: number;         // default 3
  timeoutMs?: number;        // default 120_000
}

export class CodexLLM {
  constructor(opts?: CodexOptions);
  complete(prompt: string): Promise<string>;
  completeJson(prompt: string): Promise<Record<string, unknown>>;
  completeJsonSchema(prompt: string, jsonSchema: object): Promise<Record<string, unknown>>;
}

export class DryRunLLM {
  readonly captured: string[];
  complete(prompt: string): Promise<string>;               // records, returns ""
  completeJson(prompt: string): Promise<Record<string, unknown>>;        // records, returns {}
  completeJsonSchema(prompt: string, jsonSchema: object): Promise<Record<string, unknown>>; // records, returns {}
}

export class SubllmError extends Error {}
export class QuotaError extends SubllmError {}
export class ClientError extends SubllmError {}
export class OutputError extends SubllmError {}
```

`completeJsonSchema` is the method your-app depends on. `complete` /
`completeJson` are ported for parity. All methods are async (subprocess-backed).

A shared `BaseLLM` shape (interface or abstract class) keeps `CodexLLM` and
`DryRunLLM` interchangeable, mirroring the Python `BaseLLM` seam.

## codex driver internals (`drivers/codexExec.ts`)

`runCodexExec(prompt, { model, reasoningEffort, search, codexHome, schemaPath, timeoutMs })`:

- argv (no shell), exactly per `drivers-contract.md`:
  `codex exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral`
  `-C <clean tmpdir> -o <tmpfile> [-m MODEL] [-c model_reasoning_effort="EFFORT"]`
  `[-c web_search="live"] [--output-schema SCHEMA_FILE] -- PROMPT`
- **True context isolation needs `-C <clean temp dir>`**, not just CODEX_HOME:
  `--ignore-user-config` only skips `$CODEX_HOME/config.toml`; codex still loads
  a project `AGENTS.md` from its working directory. So each call runs in a fresh
  empty temp dir (also `cwd`), removed afterward. `--ephemeral` avoids persisting
  session files. (The Python driver predates this and does not yet pass
  `-C`/`--ephemeral` — a known gap to backfill; see `docs/drivers-contract.md`.)
- `CODEX_HOME` set via the child env when `codexHome` is given (otherwise inherit).
- Model string passed **verbatim**.
- Run via `execFile` with `timeout: timeoutMs`; on timeout the child is killed →
  `ClientError`. Missing binary (`ENOENT`) → `ClientError`.
- On non-zero exit: inspect `stderr + "\n" + stdout`; if it matches
  `/usage limit|rate.?limit|quota|too many requests/i` → `QuotaError`, else
  `ClientError`. ("try again later" is intentionally excluded — too generic; it
  would misclassify auth/network failures as quota. Matches the Python note.)
- On success: read and return the `-o` output file's contents.

## Parallel-call safety

your-app may invoke the client concurrently (today up to 5). Each call:

- mints its **own** temp output file via `node:crypto.randomUUID()` under
  `os.tmpdir()` (e.g. `subllm-codex-<uuid>.txt`);
- for `completeJsonSchema`, mints its **own** temp schema file the same way;
- removes both in a `finally` block.

The client instance holds only immutable config — no shared mutable state.
N concurrent calls = N independent processes with N distinct temp files. No
collisions on temp files, output files, or auth.

## Structured output (enforced validation)

`completeJsonSchema(prompt, jsonSchema)`:

1. Serialize the caller's JSON Schema to a temp file; pass `--output-schema`.
2. Parse the output file as JSON; require a non-empty object (else `OutputError`).
3. **Validate with ajv** against the same schema. The SDK accepts **standard
   JSON Schema**; ajv (`strict: false`) ignores unknown/vendor keywords. A schema
   ajv cannot compile (e.g. an uppercase-`type` Gemini dialect) is a caller bug,
   not retryable → `ClientError`. On validation failure of the *output* →
   `OutputError` (the binding is not trusted blindly — validation is *enforced*,
   not merely requested). Whether codex's own `--output-schema` accepts a given
   schema shape is verified by a one-off maintainer live-smoke, not unit tests.

`completeJson(prompt)` appends a JSON-only instruction to the prompt
(`"\n\nReturn ONLY a single JSON object. No prose, no code fence."`), parses, and
requires a non-empty object. `complete(prompt)` returns the raw output text.

## Retry & error classification

`retry(fn, { attempts: 3, baseDelayMs: 1000 })`:

- Exponential backoff 1s / 2s / 4s.
- Retries only `ClientError` and `OutputError` (transient).
- **Never** retries `QuotaError` — re-raises immediately (so the worker can
  pause/alert rather than burn retries on an exhausted subscription).
- Re-raises the last error after `attempts` tries.

This mirrors `base.py`'s `_retry`. There is no cross-client failover in phase 1
(that was `FallbackLLM`'s job, out of scope here).

## Testing (intent, not just behavior)

- **No real subscription calls.** A stub `codex` executable is placed on a temp
  `PATH` directory (a small script that asserts/echoes argv and writes canned
  output to the `-o` path) — the TS mirror of Python's `fake_bin` fixture.
- Cover:
  - **argv assembly:** model forwarded verbatim; `search: true` adds
    `-c web_search="live"`; `--output-schema` present only on the schema path;
    `--` precedes the prompt; `CODEX_HOME` reaches the child env.
  - **error mapping:** cap message (each quota substring) → `QuotaError`; other
    non-zero exit → `ClientError`; missing binary → `ClientError`; timeout →
    `ClientError`. A generic "try again later" stderr maps to `ClientError`, NOT
    `QuotaError` (encodes *why*: misclassifying a real failure as quota would
    silently mask it).
  - **output handling:** valid JSON object returned; empty/invalid JSON →
    `OutputError`; schema-mismatch → `OutputError` after retries.
  - **retry:** transient `ClientError`/`OutputError` retried with backoff;
    `QuotaError` is NOT retried (asserts attempt count == 1).
  - **parallel safety:** ≥5 concurrent calls each get distinct temp/output files
    and all succeed (encodes the your-app batch requirement).
  - **dry-run:** `DryRunLLM` records prompts in `captured`, spawns no process.

## How your-app consumes it

- Local **`file:` / path dependency**, mirroring the Python `uv add /path` model:
  `"subllm": "file:/path/to/subllm/ts"` in your-app's
  `package.json`.
- Named ESM imports with types: `import { CodexLLM, QuotaError } from "subllm"`.
- Built with `tsc` to `dist/` (`.js` + `.d.ts`); the `exports` map points at the
  built entry.

## Acceptance criteria (from the requirements brief)

1. `new CodexLLM({ model, search, codexHome }).completeJsonSchema(prompt, jsonSchemaObject)`
   returns a parsed object satisfying the schema, from a real `codex exec` run.
2. `search: true` causes codex to perform web search for that call.
3. The model string reaches `codex exec -m` unaltered.
4. A subscription-limit failure surfaces as `QuotaError` (distinct) and is not
   retried.
5. Transient client/output failures retry with backoff; schema-mismatch raises
   `OutputError` after retries.
6. ≥5 concurrent calls succeed without temp/output/auth collisions.
7. `DryRunLLM` lets your-app unit-test its pipeline with no codex process and no
   quota spend.
8. Consumable by your-app as a typed `file:`/path dependency with named ESM
   exports.

## Out of scope (phase 1)

- `ClaudeLLM` / tmux driver.
- `FallbackLLM`.
- `RegionGuard` / `RegionError`.
- A TypeScript CLI (your-app imports the package directly).
- Streaming responses; CJS/browser builds.
- Any HTTP server / proxy (forbidden by the hard constraint).
