# Codex review trail — TypeScript SDK implementation — 2026-06-02

Per-task codex reviews run during subagent-driven implementation of
`docs/superpowers/plans/2026-06-02-subllm-ts-sdk.md`. Each review ran `codex exec`
read-only in an isolated `CODEX_HOME` (auth.json copy), grounded against the real
installed **codex-cli 0.133.0** and **Node v24.14.0**. This complements the
plan-level review in `codex-ts-review-2026-06-02.md`.

Workflow per task: implementer (TDD, self-review) → spec-compliance review →
code-quality review → codex review (grounded against real tools). Fixes were
applied as follow-up commits (history not rewritten).

## Findings applied (fixed during implementation)

- **Task 4 / CRITICAL — ajv import did not type-check.** `import Ajv from "ajv"` +
  `new Ajv()` passed vitest (esbuild strips types) but failed `tsc` with `TS2351:
  not constructable` under `module: NodeNext` — which would break the `prepare`
  (`tsc`) step every `file:` consumer runs. Fixed: `import { Ajv } from "ajv"`
  (named export; verified `tsc` clean + runtime construct). Process change: every
  TS task now runs BOTH `npm test` AND `npm run build`, since tests don't type-check.
- **Task 4 / SHOULD-FIX — QuotaError could be retried via `retryOn` override.** Added
  a hard guard in `retry()` (`if (e instanceof QuotaError) throw e`) so the
  "never retry an exhausted subscription" contract is unbreakable regardless of options.
- **Task 4 / SHOULD-FIX — unbounded ajv schema cache.** ajv caches `$id`-less schemas
  by object identity, growing per call. `compileSchema` now `removeSchema(jsonSchema)`
  right after compile (validator stays usable) → no unbounded growth.
- **Task 4 / NICE — `attempts` not validated.** `NaN`/fractional bypassed `attempts<1`.
  Now `!Number.isSafeInteger(attempts) || attempts<1 → RangeError`.
- **Task 5 / CRITICAL — codex exec hangs on stdin.** `codex exec` 0.133.0 reads stdin
  even when the prompt is an argv arg; promisified `execFile` left stdin an open pipe,
  so real calls printed "Reading additional input from stdin..." and hung until the
  120s timeout. (Tests passed only because the bash stub ignores stdin.) Fixed:
  `exec.child.stdin?.end()` (via the `PromiseWithChild.child` handle) + a regression
  test using a stdin-reading stub.
- **Task 5 / Minor — `killed`→"timed out" message.** `err.killed` is true for any
  signal kill, not only timeout; message reworded to not mislead.
- **Task 6 / Minor — schema-file write outside its cleanup `try`.** Moved `writeFile`
  inside the `try` whose `finally` removes it, so a failed write can't leak the file.
- **Final / coverage — retry-recovery seam untested.** Added an integration test proving
  `CodexLLM.complete()` recovers on a later attempt after a transient driver ClientError
  (file-based counter across process spawns asserts attempt 2 succeeds).

## Validated correct against codex-cli 0.133.0 (no change needed)

- All flags accepted: `-s read-only`, `--skip-git-repo-check`, `--ignore-user-config`,
  `--ephemeral`, `-C/--cd <dir>`, `-o <file>`, `-m <model>`, `-c key=value`,
  `--output-schema <file>`. `--search` is NOT valid (the driver correctly uses
  `-c web_search="live"` as a single argv element).
- `-o` writes the **final agent message as plain text** (not JSONL/transcript), so
  reading the `-o` file is the right contract for `complete()`.
- `--output-schema` describes the model's final response shape; reading the last
  message (not the first JSONL event) is correct (intermediate messages can be
  schema-shaped; only the last is final).
- ajv default build (draft-07, `strict:false`) validates your-app's standard
  lowercase JSON Schema (object/string/array/integer, required, properties, enum);
  `Ajv2020` not needed.
- Error-subclass pattern correct at `target: ES2022` (instanceof-based retry
  classification verified by runtime probe).
- Package wiring: `import { CodexLLM, QuotaError } from "subllm"` resolves from built
  `dist/` for an ESM/NodeNext or bundler `file:` consumer (`exports`/`types`/`files`
  coherent); empirically confirmed by the Task 8 consumer smoke (`function function`).

## Deferred — known limitations / recommended follow-ups (NOT fixed; surfaced)

These are real but were deliberately NOT changed at the finish line, to preserve
parity with the authoritative Python driver and avoid unverifiable finish-line edits.
Track for a future cycle (coordinate the driver-level ones with Python so the two
implementations don't diverge):

1. **Large prompts via argv → `ARG_MAX`/`E2BIG`.** The driver passes the prompt as an
   argv element (`-- <prompt>`), matching the Python driver. A prompt near/over the OS
   arg limit (~1 MB on macOS, less after env overhead) fails with a clean (but uselessly
   3×-retried) ClientError before codex starts. your-app's summarization prompts are
   well under this in practice. Follow-up: feed the prompt via stdin (codex supports
   stdin when the prompt is omitted/`-`), applied to BOTH the TS and Python drivers.
2. **`--ignore-rules` not passed.** 0.133.0 `exec` supports `--ignore-rules` to skip
   user/project execpolicy `.rules`. The driver isolates config (`--ignore-user-config`),
   cwd/AGENTS.md (`-C` clean dir), and session files (`--ephemeral`) but not rules. Adding
   it would complete isolation, but it could not be live-validated here (no quota), and a
   wrong flag would break every real call — so defer until a live smoke confirms it, then
   add to both drivers.
3. **`search: true` + `completeJsonSchema`.** Structured output combined with
   tools/web-search is less proven on 0.133.0 (reports of markdown-wrapped output).
   Live-smoke that exact combination before relying on it in production.
4. **`completeJson` is best-effort** (no code-fence stripping; documented in README).
   The reliable structured path is `completeJsonSchema`. Matches the Python contract.
5. **Empty `{}` rejected before ajv** in `parseJsonObject` — correct for your-app's
   required-field schemas, technically wrong for a schema that permits `{}`. Matches
   Python; revisit only if a consumer needs empty-object outputs.

## Cross-language divergence (TS ahead of Python)

The TS driver is intentionally ahead of the Python driver on isolation/robustness:
it passes `-C <clean cwd>` + `--ephemeral` and closes the child's stdin; the Python
driver (`python/src/subllm/drivers/codex_exec.py`) does neither yet. Flagged in
`docs/drivers-contract.md` as a Python backfill TODO. Do not silently fork further —
fold the deferred driver-level items above into both implementations together.
