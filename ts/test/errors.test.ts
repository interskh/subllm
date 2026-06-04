import { describe, it, expect } from "vitest";
import {
  SubllmError,
  QuotaError,
  ClientError,
  OutputError,
  RegionError,
} from "../src/errors.js";

describe("errors", () => {
  it("Quota/Client/Output/Region all subclass SubllmError", () => {
    for (const E of [QuotaError, ClientError, OutputError, RegionError]) {
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
