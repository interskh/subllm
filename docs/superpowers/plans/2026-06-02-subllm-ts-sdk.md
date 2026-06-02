# subllm TypeScript SDK Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a TypeScript/Node ESM SDK that drives `codex exec` behind a `CodexLLM` class with the same capabilities as Python subllm's `CodexLLM` — structured JSON output against a caller-supplied JSON Schema, web search toggle, verbatim model selection, CODEX_HOME isolation, the Quota/Client/Output error hierarchy, transient retry, and parallel-call safety — so the `your-app` project can move both its LLM calls onto flat-rate ChatGPT-subscription quota.

**Architecture:** A `BaseLLM` interface with three async methods (`complete`, `completeJson`, `completeJsonSchema`). One concrete client, `CodexLLM`, wraps a thin subprocess driver (`runCodexExec`) that invokes the real `codex exec` CLI via `node:child_process.execFile` (argv array, no shell). Structured output is validated with **ajv** against the caller's JSON Schema (enforced, not trusted). A `DryRunLLM` records prompts for consumers' tests. The `drivers/` file is the isolated subprocess boundary — the TS realization of the same `codex_exec` contract the Python driver implements.

**Tech Stack:** TypeScript 5.x, Node ≥20 (ESM, `"type": "module"`), built with `tsc` to `dist/`. `vitest` for tests. Runtime dep: `ajv`. Tests use a stub `codex` executable on `PATH` — never real subscription calls.

**Read before starting:**
- `docs/superpowers/specs/2026-06-02-subllm-ts-sdk-design.md` (authoritative spec).
- `docs/drivers-contract.md` — the `codex_exec` section: exact argv, `web_search="live"` override (NOT `--search`), output-file reading, quota-vs-client classification.
- `python/src/subllm/codex.py`, `python/src/subllm/drivers/codex_exec.py`, `python/src/subllm/base.py`, `python/src/subllm/errors.py` — the Python contract being ported. (These paths assume Task 1's restructure has run.)
- `/tmp/subllm-ts-sdk-requirements.md` — the consumer (`your-app`) requirements brief.

**Conventions:**
- All TS relative imports use explicit `.js` extensions (required by `module: NodeNext`). e.g. `import { ClientError } from "./errors.js"`.
- `npm --prefix ts <script>` to run package scripts without changing directory.
- Commit after every task (frequent commits).
- No real `codex`/network calls in tests.
- Match names exactly across tasks: methods `complete`/`completeJson`/`completeJsonSchema`; errors `SubllmError`/`QuotaError`/`ClientError`/`OutputError`; driver `runCodexExec`; helpers `retry`/`compileSchema`/`validateWithSchema`/`parseJsonObject`.

---

## Phase 0 — Repo restructure (polyglot layout)

### Task 1: Move the Python package into `python/`

**Files:**
- Move: `src/` → `python/src/`, `tests/` → `python/tests/`, `pyproject.toml` → `python/pyproject.toml`, `uv.lock` → `python/uv.lock`
- Modify: `README.md` (Python consumer path)

- [ ] **Step 1: Create `python/` and git-mv the Python tree into it**

```bash
mkdir -p python
git mv src python/src
git mv tests python/tests
git mv pyproject.toml python/pyproject.toml
git mv uv.lock python/uv.lock
rm -rf .venv .pytest_cache   # untracked, regenerated under python/ on next sync
```

(`python/pyproject.toml` keeps `packages = ["src/subllm"]` — that path is relative to the pyproject's new location, so it now resolves to `python/src/subllm`. No edit needed.)

- [ ] **Step 2: Fix the Python consumer paths in `README.md`**

Three exact string replacements (the documented install path gains `/python`):

1. `uv add /path/to/subllm` → `uv add /path/to/subllm/python`
2. `subllm = { path = "/path/to/subllm" }` → `subllm = { path = "/path/to/subllm/python" }`
3. `uv pip install -e /path/to/subllm` → `uv pip install -e /path/to/subllm/python`

(The README's full layout tree + a TypeScript section are rewritten in Task 7, once the SDK exists.)

- [ ] **Step 3: Verify the Python suite still passes from the new location**

Run: `uv run --directory python pytest -q`
Expected: `46 passed` (the existing green suite, now rooted at `python/`).

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "refactor: move Python package into python/ for polyglot (python/ + ts/) layout"
```

(The `git mv` renames are already staged; `git add README.md` adds the path edits.)

---

## Phase 1 — TypeScript foundation

### Task 2: TS package skeleton

**Files:**
- Create: `ts/package.json`
- Create: `ts/tsconfig.json`
- Create: `ts/vitest.config.ts`
- Create: `ts/.gitignore`
- Create: `ts/src/index.ts` (placeholder; real exports land in Task 6)
- Modify: `.gitignore` (root — add `node_modules/`)

- [ ] **Step 1: Write `ts/package.json`**

```json
{
  "name": "subllm",
  "version": "0.1.0",
  "description": "Drive codex exec from TypeScript on flat-rate ChatGPT-subscription quota (client-native, no proxy).",
  "type": "module",
  "engines": { "node": ">=20" },
  "exports": {
    ".": {
      "types": "./dist/index.d.ts",
      "import": "./dist/index.js"
    }
  },
  "types": "./dist/index.d.ts",
  "files": ["dist"],
  "scripts": {
    "build": "tsc",
    "prepare": "tsc",
    "test": "vitest run",
    "test:watch": "vitest"
  },
  "dependencies": {
    "ajv": "^8.17.1"
  },
  "devDependencies": {
    "typescript": "^5.6.0",
    "vitest": "^2.1.0",
    "@types/node": "^20.16.0"
  }
}
```

- [ ] **Step 2: Write `ts/tsconfig.json`**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "strict": true,
    "declaration": true,
    "outDir": "dist",
    "rootDir": "src",
    "esModuleInterop": true,
    "skipLibCheck": true,
    "forceConsistentCasingInFileNames": true
  },
  "include": ["src"]
}
```

- [ ] **Step 3: Write `ts/vitest.config.ts`**

```ts
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
  },
});
```

- [ ] **Step 4: Write `ts/.gitignore`**

```
node_modules/
dist/
```

- [ ] **Step 5: Write the placeholder `ts/src/index.ts`**

```ts
export {};
```

- [ ] **Step 6: Add `node_modules/` to the root `.gitignore`**

Append a line `node_modules/` to `.gitignore` (the existing `dist/` line already ignores `ts/dist/`).

- [ ] **Step 7: Install dependencies**

Run: `npm --prefix ts install`
Expected: creates `ts/node_modules` and `ts/package-lock.json`; the `prepare` script runs `tsc` and emits `ts/dist/index.js`.

- [ ] **Step 8: Verify the build works**

Run: `npm --prefix ts run build`
Expected: exits 0; `ts/dist/index.js` and `ts/dist/index.d.ts` exist.
(Do NOT run `vitest` yet — there are no tests, and vitest exits non-zero on an empty suite. The first test arrives in Task 3.)

- [ ] **Step 9: Commit**

```bash
git add ts/package.json ts/tsconfig.json ts/vitest.config.ts ts/.gitignore ts/src/index.ts ts/package-lock.json .gitignore
git commit -m "chore(ts): package skeleton (ESM, tsc, vitest, ajv)"
```

---

### Task 3: Error types

**Files:**
- Create: `ts/src/errors.ts`
- Test: `ts/test/errors.test.ts`

- [ ] **Step 1: Write the failing test**

`ts/test/errors.test.ts`:
```ts
import { describe, it, expect } from "vitest";
import { SubllmError, QuotaError, ClientError, OutputError } from "../src/errors.js";

describe("errors", () => {
  it("Quota/Client/Output all subclass SubllmError", () => {
    for (const E of [QuotaError, ClientError, OutputError]) {
      expect(new E("x")).toBeInstanceOf(SubllmError);
    }
  });

  it("are real Errors, carry the message, and set name to the subclass", () => {
    const e = new QuotaError("5h cap reached");
    expect(e).toBeInstanceOf(Error);
    expect(e.message).toContain("5h cap");
    expect(e.name).toBe("QuotaError");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm --prefix ts test`
Expected: FAIL — cannot resolve `../src/errors.js` (module does not exist yet).

- [ ] **Step 3: Write minimal implementation**

`ts/src/errors.ts`:
```ts
/** subllm exception hierarchy. All errors subclass SubllmError. */

export class SubllmError extends Error {
  constructor(message?: string) {
    super(message);
    // new.target is the concrete subclass being constructed.
    this.name = new.target.name;
    // Keep instanceof correct across the prototype chain.
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

/** Subscription quota exhausted (codex 5h/weekly cap). Not retried; the signal
 *  for a caller to pause/alert rather than treat the failure as a bug. */
export class QuotaError extends SubllmError {}

/** Subprocess failed: missing binary, crash, timeout, auth missing/invalid. */
export class ClientError extends SubllmError {}

/** Model output unusable: empty, invalid JSON, or schema-validation mismatch. */
export class OutputError extends SubllmError {}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm --prefix ts test`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add ts/src/errors.ts ts/test/errors.test.ts
git commit -m "feat(ts): error hierarchy"
```

---

### Task 4: BaseLLM, retry, schema validation, DryRunLLM

**Files:**
- Create: `ts/src/base.ts`
- Test: `ts/test/base.test.ts`

- [ ] **Step 1: Write the failing test**

`ts/test/base.test.ts`:
```ts
import { describe, it, expect } from "vitest";
import { retry, compileSchema, validateWithSchema, DryRunLLM } from "../src/base.js";
import { ClientError, OutputError, QuotaError } from "../src/errors.js";

describe("retry", () => {
  it("succeeds after transient failures", async () => {
    let n = 0;
    const r = await retry(
      async () => {
        n++;
        if (n < 3) throw new OutputError("bad json");
        return "ok";
      },
      { attempts: 3, baseDelayMs: 0 },
    );
    expect(r).toBe("ok");
    expect(n).toBe(3);
  });

  it("does NOT retry QuotaError (raised immediately)", async () => {
    let n = 0;
    await expect(
      retry(
        async () => {
          n++;
          throw new QuotaError("cap");
        },
        { attempts: 3, baseDelayMs: 0 },
      ),
    ).rejects.toBeInstanceOf(QuotaError);
    expect(n).toBe(1);
  });

  it("reraises the last error after exhausting attempts", async () => {
    await expect(
      retry(async () => {
        throw new ClientError("crash");
      }, { attempts: 2, baseDelayMs: 0 }),
    ).rejects.toBeInstanceOf(ClientError);
  });
});

describe("schema validation", () => {
  const schema = {
    type: "object",
    required: ["topic"],
    properties: { topic: { type: "string" } },
  };

  it("passes valid data through unchanged", () => {
    const validate = compileSchema(schema);
    expect(validateWithSchema({ topic: "x" }, validate)).toEqual({ topic: "x" });
  });

  it("throws OutputError on a schema mismatch", () => {
    const validate = compileSchema(schema);
    expect(() => validateWithSchema({ wrong: 1 }, validate)).toThrow(OutputError);
  });
});

describe("DryRunLLM", () => {
  it("records prompts and makes no real call", async () => {
    const llm = new DryRunLLM();
    expect(await llm.complete("a")).toBe("");
    expect(await llm.completeJson("b")).toEqual({});
    expect(await llm.completeJsonSchema("c", { type: "object" })).toEqual({});
    expect(llm.captured).toEqual(["a", "b", "c"]);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm --prefix ts test`
Expected: FAIL — cannot resolve `../src/base.js`.

- [ ] **Step 3: Write minimal implementation**

`ts/src/base.ts`:
```ts
/** BaseLLM contract, transient-retry helper, ajv-backed schema validation, and a
 *  no-op DryRunLLM. */
import { setTimeout as sleep } from "node:timers/promises";
import Ajv, { type ValidateFunction } from "ajv";
import { ClientError, OutputError } from "./errors.js";

export interface BaseLLM {
  complete(prompt: string): Promise<string>;
  completeJson(prompt: string): Promise<Record<string, unknown>>;
  completeJsonSchema(
    prompt: string,
    jsonSchema: object,
  ): Promise<Record<string, unknown>>;
}

type ErrCtor = new (...args: never[]) => Error;

// Transient failures worth retrying within a single client. QuotaError is
// intentionally excluded — retrying an exhausted subscription is pointless.
const DEFAULT_RETRY_ON: ErrCtor[] = [ClientError, OutputError];

export interface RetryOptions {
  attempts?: number;
  baseDelayMs?: number;
  retryOn?: ErrCtor[];
}

export async function retry<T>(
  fn: () => Promise<T>,
  opts: RetryOptions = {},
): Promise<T> {
  const { attempts = 3, baseDelayMs = 1000, retryOn = DEFAULT_RETRY_ON } = opts;
  let last: unknown;
  for (let i = 0; i < attempts; i++) {
    try {
      return await fn();
    } catch (e) {
      if (!retryOn.some((E) => e instanceof E)) throw e; // non-retryable -> propagate
      last = e;
      if (i < attempts - 1 && baseDelayMs) await sleep(baseDelayMs * 2 ** i);
    }
  }
  throw last;
}

// One shared validator factory. compile() with no $id does not mutate the
// instance cache, so per-call compilation is safe and leak-free.
const ajv = new Ajv({ strict: false, allErrors: true });

/** Compile a caller-supplied JSON Schema into a reusable validator. A schema
 *  that ajv cannot compile is a caller bug (not retryable) -> ClientError. */
export function compileSchema(jsonSchema: object): ValidateFunction {
  try {
    return ajv.compile(jsonSchema);
  } catch (e) {
    throw new ClientError(
      `invalid JSON schema passed to completeJsonSchema: ${String(e)}`,
    );
  }
}

/** Enforce the schema on parsed output; raise OutputError on mismatch. The TS
 *  analog of Pydantic's model_validate — structured output is enforced, not
 *  merely requested. */
export function validateWithSchema(
  data: Record<string, unknown>,
  validate: ValidateFunction,
): Record<string, unknown> {
  if (!validate(data)) {
    throw new OutputError(
      `output failed schema validation: ${ajv.errorsText(validate.errors)}`,
    );
  }
  return data;
}

/** No-op LLM: records prompts it would have sent. For consumers' tests. */
export class DryRunLLM implements BaseLLM {
  readonly captured: string[] = [];

  async complete(prompt: string): Promise<string> {
    this.captured.push(prompt);
    return "";
  }

  async completeJson(prompt: string): Promise<Record<string, unknown>> {
    this.captured.push(prompt);
    return {};
  }

  async completeJsonSchema(
    prompt: string,
    _jsonSchema: object,
  ): Promise<Record<string, unknown>> {
    this.captured.push(prompt);
    return {};
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm --prefix ts test`
Expected: PASS (errors + base = 8 tests total).

NOTE for reviewers: `import Ajv from "ajv"` relies on `esModuleInterop`. If the installed ajv version requires a different ESM import form (`import { Ajv } from "ajv"`), correct it here — see the "Runtime specifics for codex review" section. The implementer must confirm `npm --prefix ts test` actually passes, not assume the import form.

- [ ] **Step 5: Commit**

```bash
git add ts/src/base.ts ts/test/base.test.ts
git commit -m "feat(ts): BaseLLM contract, retry, ajv schema validation, DryRunLLM"
```

---

## Phase 2 — Codex client

### Task 5: codex exec driver

**Files:**
- Create: `ts/src/drivers/codexExec.ts`
- Create: `ts/test/helpers.ts` (fake-codex installer; the TS mirror of Python's `fake_bin` fixture)
- Test: `ts/test/codexExec.test.ts`

- [ ] **Step 1: Write the fake-codex test helper**

`ts/test/helpers.ts`:
```ts
import { mkdtempSync, writeFileSync, chmodSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

/** Write a fake `codex` executable into a fresh temp dir and prepend that dir to
 *  PATH. Returns a cleanup fn that restores PATH and removes the dir. The child
 *  inherits process.env (incl. the prepended PATH) via runCodexExec. */
export function installFakeCodex(script: string): () => void {
  const dir = mkdtempSync(join(tmpdir(), "subllm-fakebin-"));
  const bin = join(dir, "codex");
  writeFileSync(bin, script);
  chmodSync(bin, 0o755);
  const prevPath = process.env.PATH ?? "";
  process.env.PATH = `${dir}:${prevPath}`;
  return () => {
    process.env.PATH = prevPath;
    rmSync(dir, { recursive: true, force: true });
  };
}

/** A stub that writes `text` to whatever path follows `-o`. */
export function echoStub(text: string): string {
  return `#!/bin/bash
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
printf '%s' '${text}' > "$out"
`;
}
```

- [ ] **Step 2: Write the failing test**

`ts/test/codexExec.test.ts`:
```ts
import { describe, it, expect } from "vitest";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runCodexExec } from "../src/drivers/codexExec.js";
import { ClientError, QuotaError } from "../src/errors.js";
import { installFakeCodex, echoStub } from "./helpers.js";

describe("runCodexExec", () => {
  it("returns the contents of the -o output file", async () => {
    const cleanup = installFakeCodex(echoStub("a tidy summary"));
    try {
      expect(await runCodexExec("summarize this")).toBe("a tidy summary");
    } finally {
      cleanup();
    }
  });

  it("forwards the model verbatim and adds search + output-schema flags", async () => {
    const argvLog = join(mkdtempSync(join(tmpdir(), "argv-")), "argv.txt");
    const cleanup = installFakeCodex(`#!/bin/bash
echo "$@" > "${argvLog}"
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
printf '{"ok":true}' > "$out"
`);
    try {
      await runCodexExec("x", {
        model: "gpt-5.4-mini",
        search: true,
        schemaPath: "/tmp/s.json",
      });
      const argv = readFileSync(argvLog, "utf8");
      expect(argv).toContain("-m gpt-5.4-mini"); // verbatim, not normalized
      expect(argv).toContain('web_search="live"');
      expect(argv).not.toContain("--search");
      expect(argv).toContain("--output-schema /tmp/s.json");
    } finally {
      cleanup();
    }
  });

  it("maps a usage-limit message to QuotaError", async () => {
    const cleanup = installFakeCodex(`#!/bin/bash
echo "You have hit your usage limit." >&2
exit 1
`);
    try {
      await expect(runCodexExec("x")).rejects.toBeInstanceOf(QuotaError);
    } finally {
      cleanup();
    }
  });

  it("does NOT map generic 'try again later' to QuotaError", async () => {
    const cleanup = installFakeCodex(`#!/bin/bash
echo "network error, try again later" >&2
exit 1
`);
    try {
      await expect(runCodexExec("x")).rejects.toBeInstanceOf(ClientError);
    } finally {
      cleanup();
    }
  });

  it("maps a missing binary to ClientError", async () => {
    const prev = process.env.PATH;
    process.env.PATH = mkdtempSync(join(tmpdir(), "empty-")); // no codex here
    try {
      await expect(runCodexExec("x")).rejects.toBeInstanceOf(ClientError);
    } finally {
      process.env.PATH = prev;
    }
  });

  it("maps a timeout to ClientError", async () => {
    const cleanup = installFakeCodex(`#!/bin/bash
sleep 5
`);
    try {
      await expect(
        runCodexExec("x", { timeoutMs: 200 }),
      ).rejects.toBeInstanceOf(ClientError);
    } finally {
      cleanup();
    }
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `npm --prefix ts test`
Expected: FAIL — cannot resolve `../src/drivers/codexExec.js`.

- [ ] **Step 4: Write minimal implementation**

`ts/src/drivers/codexExec.ts`:
```ts
/** Invoke `codex exec` (ChatGPT-subscription auth) as a clean text/JSON engine.
 *  Runs read-only and config-isolated to avoid the AGENTS.md context-pollution
 *  gotcha. The isolated subprocess boundary — see docs/drivers-contract.md. */
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { randomUUID } from "node:crypto";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { readFile, rm } from "node:fs/promises";
import { ClientError, QuotaError } from "../errors.js";

const execFileP = promisify(execFile);

// stderr/stdout substrings that mean "out of subscription budget", not a bug.
// "try again later" is intentionally NOT here — too generic; it would
// misclassify auth/network failures as quota, and QuotaError must stay precise.
const QUOTA_RE = /usage limit|rate.?limit|quota|too many requests/i;

export interface CodexExecOptions {
  model?: string;
  reasoningEffort?: string;
  search?: boolean;
  codexHome?: string;
  schemaPath?: string;
  timeoutMs?: number;
}

/** Run one non-interactive codex turn; return the final message text.
 *  Raises QuotaError on a subscription-limit message, ClientError otherwise. */
export async function runCodexExec(
  prompt: string,
  opts: CodexExecOptions = {},
): Promise<string> {
  const {
    model,
    reasoningEffort,
    search = false,
    codexHome,
    schemaPath,
    timeoutMs = 120_000,
  } = opts;

  const outPath = join(tmpdir(), `subllm-codex-${randomUUID()}.txt`);
  const argv = [
    "exec",
    "-s",
    "read-only",
    "--skip-git-repo-check",
    "--ignore-user-config",
    "-o",
    outPath,
  ];
  if (model) argv.push("-m", model); // verbatim — never normalized
  if (reasoningEffort) {
    argv.push("-c", `model_reasoning_effort="${reasoningEffort}"`);
  }
  // NOTE: `--search` is NOT a valid codex exec flag; web search is a config override.
  if (search) argv.push("-c", `web_search="live"`);
  if (schemaPath) argv.push("--output-schema", schemaPath);
  argv.push("--", prompt); // -- so a prompt starting with '-' isn't parsed as a flag

  const env = { ...process.env };
  if (codexHome) env.CODEX_HOME = codexHome;

  try {
    try {
      await execFileP("codex", argv, {
        env,
        timeout: timeoutMs,
        maxBuffer: 10 * 1024 * 1024,
      });
    } catch (e: unknown) {
      const err = e as NodeJS.ErrnoException & {
        stdout?: string;
        stderr?: string;
        killed?: boolean;
      };
      if (err.code === "ENOENT") {
        throw new ClientError("codex binary not found on PATH");
      }
      if (err.killed) {
        throw new ClientError(`codex exec timed out after ${timeoutMs}ms`);
      }
      const blob = `${err.stderr ?? ""}\n${err.stdout ?? ""}`.trim();
      if (QUOTA_RE.test(blob)) {
        throw new QuotaError(`codex subscription limit: ${blob.slice(0, 200)}`);
      }
      throw new ClientError(
        `codex exec failed (exit ${String(err.code)}): ${blob.slice(0, 200)}`,
      );
    }
    return await readFile(outPath, "utf8");
  } finally {
    await rm(outPath, { force: true });
  }
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `npm --prefix ts test`
Expected: PASS (errors + base + codexExec = 14 tests total).

- [ ] **Step 6: Commit**

```bash
git add ts/src/drivers/codexExec.ts ts/test/helpers.ts ts/test/codexExec.test.ts
git commit -m "feat(ts): codex exec driver (argv contract, error classification, parallel-safe temp file)"
```

---

### Task 6: CodexLLM + public exports

**Files:**
- Create: `ts/src/codex.ts`
- Modify: `ts/src/index.ts`
- Test: `ts/test/codex.test.ts`

- [ ] **Step 1: Write the failing test**

`ts/test/codex.test.ts`:
```ts
import { describe, it, expect } from "vitest";
import { CodexLLM } from "../src/codex.js";
import { OutputError } from "../src/errors.js";
import { installFakeCodex, echoStub } from "./helpers.js";

const SCHEMA = {
  type: "object",
  required: ["topic"],
  properties: { topic: { type: "string" } },
};

describe("CodexLLM", () => {
  it("complete returns the model text", async () => {
    const cleanup = installFakeCodex(echoStub("hello world"));
    try {
      const llm = new CodexLLM({ model: "gpt-5.4-mini" });
      expect(await llm.complete("hi")).toBe("hello world");
    } finally {
      cleanup();
    }
  });

  it("completeJson parses a JSON object", async () => {
    const cleanup = installFakeCodex(echoStub('{"topic":"weekend trip"}'));
    try {
      expect(await new CodexLLM().completeJson("x")).toEqual({
        topic: "weekend trip",
      });
    } finally {
      cleanup();
    }
  });

  it("completeJson rejects a non-object", async () => {
    const cleanup = installFakeCodex(echoStub("[]"));
    try {
      await expect(
        new CodexLLM({ attempts: 1 }).completeJson("x"),
      ).rejects.toBeInstanceOf(OutputError);
    } finally {
      cleanup();
    }
  });

  it("completeJsonSchema returns a schema-validated object", async () => {
    const cleanup = installFakeCodex(echoStub('{"topic":"ok"}'));
    try {
      expect(await new CodexLLM().completeJsonSchema("x", SCHEMA)).toEqual({
        topic: "ok",
      });
    } finally {
      cleanup();
    }
  });

  it("completeJsonSchema rejects output that violates the schema", async () => {
    const cleanup = installFakeCodex(echoStub('{"wrong":"field"}'));
    try {
      await expect(
        new CodexLLM({ attempts: 1 }).completeJsonSchema("x", SCHEMA),
      ).rejects.toBeInstanceOf(OutputError);
    } finally {
      cleanup();
    }
  });

  it("survives >=5 concurrent calls without temp/output-file collisions", async () => {
    // The stub echoes back the prompt (the final argv element, after --). If two
    // concurrent calls shared an output file, results would cross-contaminate.
    const cleanup = installFakeCodex(`#!/bin/bash
args=("$@")
out=""
for ((i=0; i<\${#args[@]}; i++)); do
  [ "\${args[$i]}" = "-o" ] && out="\${args[$((i+1))]}"
done
prompt="\${args[\${#args[@]}-1]}"
printf '%s' "$prompt" > "$out"
`);
    try {
      const llm = new CodexLLM();
      const results = await Promise.all(
        [0, 1, 2, 3, 4].map((i) => llm.complete(`p${i}`)),
      );
      expect([...results].sort()).toEqual(["p0", "p1", "p2", "p3", "p4"]);
    } finally {
      cleanup();
    }
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm --prefix ts test`
Expected: FAIL — cannot resolve `../src/codex.js`.

- [ ] **Step 3: Write minimal implementation**

`ts/src/codex.ts`:
```ts
/** CodexLLM — wraps `codex exec` behind the BaseLLM contract. */
import { randomUUID } from "node:crypto";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { writeFile, rm } from "node:fs/promises";
import type { BaseLLM } from "./base.js";
import { retry, compileSchema, validateWithSchema } from "./base.js";
import { runCodexExec } from "./drivers/codexExec.js";
import { OutputError } from "./errors.js";

const JSON_INSTRUCTION =
  "\n\nReturn ONLY a single JSON object. No prose, no code fence.";

export interface CodexOptions {
  model?: string;
  reasoningEffort?: string;
  search?: boolean;
  codexHome?: string;
  attempts?: number;
  timeoutMs?: number;
}

export class CodexLLM implements BaseLLM {
  private readonly model?: string;
  private readonly reasoningEffort: string;
  private readonly search: boolean;
  private readonly codexHome?: string;
  private readonly attempts: number;
  private readonly timeoutMs: number;

  constructor(opts: CodexOptions = {}) {
    this.model = opts.model;
    this.reasoningEffort = opts.reasoningEffort ?? "medium";
    this.search = opts.search ?? false;
    this.codexHome = opts.codexHome;
    this.attempts = opts.attempts ?? 3;
    this.timeoutMs = opts.timeoutMs ?? 120_000;
  }

  private run(prompt: string, schemaPath?: string): Promise<string> {
    return runCodexExec(prompt, {
      model: this.model,
      reasoningEffort: this.reasoningEffort,
      search: this.search,
      codexHome: this.codexHome,
      schemaPath,
      timeoutMs: this.timeoutMs,
    });
  }

  complete(prompt: string): Promise<string> {
    return retry(() => this.run(prompt), { attempts: this.attempts });
  }

  completeJson(prompt: string): Promise<Record<string, unknown>> {
    return retry(
      async () => parseJsonObject(await this.run(prompt + JSON_INSTRUCTION)),
      { attempts: this.attempts },
    );
  }

  completeJsonSchema(
    prompt: string,
    jsonSchema: object,
  ): Promise<Record<string, unknown>> {
    const validate = compileSchema(jsonSchema); // compile once (bad schema -> ClientError)
    const schemaJson = JSON.stringify(jsonSchema);
    return retry(
      async () => {
        // Per-attempt temp schema file with a unique name -> parallel-safe.
        const schemaPath = join(
          tmpdir(),
          `subllm-schema-${randomUUID()}.json`,
        );
        await writeFile(schemaPath, schemaJson, "utf8");
        try {
          const parsed = parseJsonObject(await this.run(prompt, schemaPath));
          // Don't trust codex's binding blindly — enforce the schema.
          return validateWithSchema(parsed, validate);
        } finally {
          await rm(schemaPath, { force: true });
        }
      },
      { attempts: this.attempts },
    );
  }
}

function parseJsonObject(raw: string): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw || "{}");
  } catch {
    throw new OutputError(
      `codex returned invalid JSON: ${JSON.stringify(raw.slice(0, 120))}`,
    );
  }
  if (
    typeof parsed !== "object" ||
    parsed === null ||
    Array.isArray(parsed) ||
    Object.keys(parsed).length === 0
  ) {
    throw new OutputError("expected a non-empty JSON object");
  }
  return parsed as Record<string, unknown>;
}
```

- [ ] **Step 4: Update the public exports**

Replace `ts/src/index.ts` with:
```ts
/** subllm — subscription-native LLM clients (TypeScript SDK). */
export { CodexLLM } from "./codex.js";
export type { CodexOptions } from "./codex.js";
export { DryRunLLM } from "./base.js";
export type { BaseLLM } from "./base.js";
export {
  SubllmError,
  QuotaError,
  ClientError,
  OutputError,
} from "./errors.js";
```

- [ ] **Step 5: Run test to verify it passes**

Run: `npm --prefix ts test`
Expected: PASS (errors + base + codexExec + codex = 20 tests total).

- [ ] **Step 6: Verify the build still emits a clean dist with types**

Run: `npm --prefix ts run build`
Expected: exits 0; `ts/dist/index.d.ts` declares `CodexLLM`, `DryRunLLM`, and the four error classes.

- [ ] **Step 7: Commit**

```bash
git add ts/src/codex.ts ts/src/index.ts ts/test/codex.test.ts
git commit -m "feat(ts): CodexLLM client + public exports"
```

---

## Phase 3 — Documentation

### Task 7: Document the polyglot repo (README + AGENTS)

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Update the README layout tree to show both languages**

Replace the `## Layout` code block's tree so the top level shows `python/` and `ts/` as peers, e.g.:
```
subllm/
  python/                # Python library (CodexLLM + ClaudeLLM + FallbackLLM + CLI)
    src/subllm/...
    tests/
    pyproject.toml
  ts/                    # TypeScript SDK (CodexLLM, phase 1)
    src/
      index.ts           # public exports
      base.ts            # BaseLLM + retry + ajv validation + DryRunLLM
      errors.ts          # SubllmError / QuotaError / ClientError / OutputError
      codex.ts           # CodexLLM
      drivers/codexExec.ts  # the isolated `codex exec` subprocess boundary
    test/
    package.json
  docs/                  # shared: spec, plans, research, drivers-contract.md
  README.md
```

- [ ] **Step 2: Add a "TypeScript SDK" section to the README**

Insert after the Python usage sections (adjust the surrounding prose to taste; the content below is required):
````markdown
## TypeScript SDK (`ts/`)

A Node ESM SDK that drives `codex exec` with the same capabilities as the Python
`CodexLLM`. Phase 1 ships `CodexLLM` + `DryRunLLM` only.

Consume it as a local path dependency (mirrors the Python `uv add /path` model):

```jsonc
// your-app/package.json
{
  "dependencies": {
    "subllm": "file:/path/to/subllm/ts"
  }
}
```

```ts
import { CodexLLM, QuotaError } from "subllm";

const llm = new CodexLLM({
  model: "gpt-5.4-mini",   // forwarded verbatim to `codex exec -m`
  search: true,            // -c web_search="live"
  codexHome: "/tmp/codex-clean",
});

// classify a topic, structured against a plain JSON Schema object:
const classified = await llm.completeJsonSchema(prompt, CLASSIFIER_SCHEMA);
```

- `completeJsonSchema(prompt, jsonSchema)` accepts a **plain JSON Schema object**,
  binds it to codex via `--output-schema`, and returns a parsed object that is
  **validated with ajv** (throws `OutputError` on mismatch).
- Errors mirror the Python hierarchy: `QuotaError` (not retried), `ClientError`,
  `OutputError`, all under `SubllmError`.
- Concurrent calls are safe (each uses its own temp output/schema file).

Build/test: `npm --prefix ts install`, `npm --prefix ts run build`, `npm --prefix ts test`.
````

- [ ] **Step 3: Note the polyglot layout in `AGENTS.md`**

Add a short line near the top of `AGENTS.md` stating the repo now has two implementations: `python/` (authoritative, full feature set) and `ts/` (phase-1 `CodexLLM` SDK for `your-app`), sharing `docs/drivers-contract.md` as the cross-language contract.

- [ ] **Step 4: Verify all README doc links still resolve**

Run: `grep -oE '\]\(([^)]+\.md)\)' README.md`
Expected: every referenced `.md` path exists (the `docs/superpowers/...` and `docs/drivers-contract.md` links are unchanged by the restructure).

- [ ] **Step 5: Commit**

```bash
git add README.md AGENTS.md
git commit -m "docs: document polyglot layout + TypeScript SDK usage"
```

---

## Done criteria

- `npm --prefix ts test` → all green (20 tests).
- `npm --prefix ts run build` → clean `dist/` with `.d.ts`.
- `uv run --directory python pytest -q` → still 46 passed (restructure didn't break Python).
- A consumer can `import { CodexLLM, QuotaError } from "subllm"` via a `file:` dependency and call `completeJsonSchema(prompt, jsonSchemaObject)` to get a validated object from a real `codex exec` run.

---

## Runtime specifics for codex review (verify against the real installed tools)

These are the load-bearing assumptions most likely to be wrong — the TS analog of
the findings the Python plan's codex review caught. Ground each against the
actual installed `codex` CLI, Node ≥20, and the pinned `ajv`:

1. **ajv ESM import.** `import Ajv from "ajv"` + `new Ajv()` under
   `module: NodeNext` + `esModuleInterop: true`. Confirm this constructs (some
   ajv builds need `import { Ajv } from "ajv"` or `(await import("ajv")).default`).
   Also confirm the default ajv build's draft handling accepts **your-app's
   Gemini-style schemas** (likely no `$schema`, OpenAPI-ish keywords) under
   `strict: false` — and whether `Ajv2020` is needed instead.
2. **`codex exec --output-schema` dialect.** Verify the real `codex exec` accepts
   a Gemini-style JSON Schema object passed via `--output-schema <file>` (the
   Python side fed a Pydantic-derived draft 2020-12 schema; your-app's schemas
   may differ). If codex rejects certain schema shapes, document the constraint.
3. **`execFile` failure detection.** Confirm on Node ≥20 that a timeout yields
   `err.killed === true` (so the timeout→ClientError branch fires) and a missing
   binary yields `err.code === "ENOENT"`. Confirm `maxBuffer` (10 MB) is ample
   given codex writes the result to the `-o` file, not stdout.
4. **PATH resolution in tests.** Confirm `execFile("codex", …, { env })` resolves
   the stub via the PATH that `installFakeCodex` prepends to `process.env.PATH`
   (since `env` is spread from `process.env`). If Node resolves the binary from a
   different PATH source, the stub won't be found and tests will misbehave.
5. **`web_search="live"` argv form.** Confirm passing `-c web_search="live"` as a
   single argv element (literal quotes included) is what codex expects from a
   non-shell `execFile` invocation — matching the Python driver.
6. **`prepare` build on `file:` install.** Confirm that when your-app runs
   `npm install`, npm builds subllm's `dist/` via the `prepare` script (so the
   consumer gets compiled JS + types without a manual build step).
