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

  it("rejects attempts < 1 instead of silently throwing undefined", async () => {
    await expect(
      retry(async () => "x", { attempts: 0 }),
    ).rejects.toBeInstanceOf(RangeError);
  });

  it("never retries QuotaError even when retryOn explicitly includes it", async () => {
    let n = 0;
    await expect(
      retry(
        async () => {
          n++;
          throw new QuotaError("cap");
        },
        { attempts: 3, baseDelayMs: 0, retryOn: [QuotaError] },
      ),
    ).rejects.toBeInstanceOf(QuotaError);
    expect(n).toBe(1);
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
