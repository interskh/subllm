/** CodexLLM — wraps `codex exec` behind the BaseLLM contract. */
import { randomUUID } from "node:crypto";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { writeFile, rm } from "node:fs/promises";
import type { BaseLLM } from "./base.js";
import { retry, compileSchema, validateWithSchema } from "./base.js";
import { runCodexExec } from "./drivers/codexExec.js";
import { ClientError, OutputError } from "./errors.js";

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

  async completeJsonSchema(
    prompt: string,
    jsonSchema: object,
  ): Promise<Record<string, unknown>> {
    // async so a bad schema rejects the returned promise rather than throwing
    // synchronously (callers use .catch()/await).
    const validate = compileSchema(jsonSchema); // compile once (bad schema -> ClientError)
    let schemaJson: string;
    try {
      schemaJson = JSON.stringify(jsonSchema);
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
