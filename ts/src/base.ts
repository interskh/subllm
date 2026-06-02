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
  if (attempts < 1) throw new RangeError("retry: attempts must be >= 1");
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
