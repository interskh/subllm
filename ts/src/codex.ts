/** CodexLLM — wraps `codex exec` behind the BaseLLM contract. */
import { randomUUID } from "node:crypto";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { writeFile, rm } from "node:fs/promises";
import type { BaseLLM } from "./base.js";
import { retry, compileSchema, validateWithSchema } from "./base.js";
import { runCodexExec } from "./drivers/codexExec.js";
import { ClientError, OutputError } from "./errors.js";
import type { RegionGuard } from "./preflight.js";

const JSON_INSTRUCTION =
  "\n\nReturn ONLY a single JSON object. No prose, no code fence.";

export interface CodexOptions {
  model?: string;
  reasoningEffort?: string;
  search?: boolean;
  codexHome?: string;
  regionGuard?: RegionGuard;
  attempts?: number;
  timeoutMs?: number;
}

export class CodexLLM implements BaseLLM {
  private readonly model?: string;
  private readonly reasoningEffort: string;
  private readonly search: boolean;
  private readonly codexHome?: string;
  private readonly regionGuard?: RegionGuard;
  private readonly attempts: number;
  private readonly timeoutMs: number;

  constructor(opts: CodexOptions = {}) {
    this.model = opts.model;
    this.reasoningEffort = opts.reasoningEffort ?? "medium";
    this.search = opts.search ?? false;
    this.codexHome = opts.codexHome;
    this.regionGuard = opts.regionGuard;
    this.attempts = opts.attempts ?? 3;
    this.timeoutMs = opts.timeoutMs ?? 120_000;
  }

  private async run(prompt: string, schemaPath?: string): Promise<string> {
    if (this.regionGuard) await this.regionGuard.check(); // RegionError before any subprocess
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

  async completeJsonSchema(
    prompt: string,
    jsonSchema: object,
  ): Promise<Record<string, unknown>> {
    // async so a bad schema rejects the returned promise rather than throwing
    // synchronously (callers use .catch()/await).
    const validate = compileSchema(jsonSchema); // compile once (bad schema -> ClientError)
    let schemaJson: string;
    try {
      // codex's --output-schema runs through OpenAI strict Structured-Outputs
      // validation, which rejects a vanilla schema. Send the strict rewrite; the
      // result is still validated against the ORIGINAL (lenient) schema above, so
      // optional/nullable fields round-trip.
      schemaJson = JSON.stringify(toStrictSchema(jsonSchema));
    } catch (e) {
      throw new ClientError(
        `invalid JSON schema passed to completeJsonSchema: ${String(e)}`,
      );
    }
    return retry(
      async () => {
        // Per-attempt temp schema file with a unique name -> parallel-safe.
        const schemaPath = join(
          tmpdir(),
          `subllm-schema-${randomUUID()}.json`,
        );
        try {
          // write inside the try so a failed write is still cleaned up
          await writeFile(schemaPath, schemaJson, "utf8");
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

/** Rewrite an ordinary JSON Schema so codex's strict Structured-Outputs
 *  validator accepts it: every object node gets `additionalProperties: false`
 *  and lists all its properties as `required`, and `default: null` hints are
 *  dropped. Recurses through `$defs`/`definitions`, `properties`, `items`, and
 *  `anyOf`/`allOf`/`oneOf`/`prefixItems`. Returns a deep copy — the caller's
 *  schema object is never mutated. */
export function toStrictSchema(jsonSchema: object): object {
  const clone = structuredClone(jsonSchema);
  strictify(clone);
  return clone;
}

function strictify(node: unknown): void {
  if (Array.isArray(node)) {
    for (const item of node) strictify(item);
    return;
  }
  if (node === null || typeof node !== "object") return;
  const obj = node as Record<string, unknown>;

  // A null default would tell the model to omit a field that strict mode now
  // forces it to emit. Real (non-null) defaults are tolerated and left intact.
  if ("default" in obj && obj.default === null) delete obj.default;

  if (isPlainObject(obj.properties)) {
    obj.additionalProperties = false;
    obj.required = Object.keys(obj.properties);
  }

  for (const key of ["properties", "$defs", "definitions"]) {
    const members = obj[key];
    if (isPlainObject(members)) {
      for (const sub of Object.values(members)) strictify(sub);
    }
  }
  for (const key of ["anyOf", "allOf", "oneOf", "prefixItems", "items"]) {
    strictify(obj[key]);
  }
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === "object" && !Array.isArray(v);
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
