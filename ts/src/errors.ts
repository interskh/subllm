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
