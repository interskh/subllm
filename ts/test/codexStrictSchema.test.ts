import { describe, it, expect } from "vitest";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { CodexLLM, toStrictSchema } from "../src/codex.js";
import { installFakeCodex } from "./helpers.js";

// An ordinary JSON Schema as a consumer would write it: nested object, an
// optional field (not in `required`), and no strict-mode boilerplate.
const ORDINARY = {
  type: "object",
  required: ["title"],
  properties: {
    title: { type: "string" },
    summary: { anyOf: [{ type: "string" }, { type: "null" }], default: null },
    score: { type: "integer", default: 0 },
    tag: {
      type: "object",
      properties: { name: { type: "string" }, weight: { type: "number" } },
      required: ["name"],
    },
  },
};

describe("toStrictSchema", () => {
  it("adds additionalProperties:false to every object", () => {
    const strict = toStrictSchema(ORDINARY) as any;
    expect(strict.additionalProperties).toBe(false);
    expect(strict.properties.tag.additionalProperties).toBe(false);
  });

  it("marks all properties required, even optional/nested ones", () => {
    const strict = toStrictSchema(ORDINARY) as any;
    expect(new Set(strict.required)).toEqual(
      new Set(["title", "summary", "score", "tag"]),
    );
    expect(new Set(strict.properties.tag.required)).toEqual(
      new Set(["name", "weight"]),
    );
  });

  it("drops null defaults but keeps real ones", () => {
    const strict = toStrictSchema(ORDINARY) as any;
    expect("default" in strict.properties.summary).toBe(false);
    expect(strict.properties.score.default).toBe(0);
  });

  it("does not mutate the caller's schema object", () => {
    const before = JSON.stringify(ORDINARY);
    toStrictSchema(ORDINARY);
    expect(JSON.stringify(ORDINARY)).toBe(before);
  });
});

describe("CodexLLM.completeJsonSchema strict-mode binding", () => {
  it("sends a strict schema to codex but validates against the original", async () => {
    const seen = join(mkdtempSync(join(tmpdir(), "schema-")), "seen.json");
    // codex returns only `title`; the optional fields are absent. The original
    // (lenient) schema accepts that, proving we validate against it, not strict.
    const cleanup = installFakeCodex(`#!/bin/bash
out=""; schema=""
while [ $# -gt 0 ]; do
  [ "$1" = "-o" ] && out="$2"
  [ "$1" = "--output-schema" ] && schema="$2"
  shift
done
cp "$schema" "${seen}"
printf '{"title":"x"}' > "$out"
`);
    try {
      const result = await new CodexLLM().completeJsonSchema("x", ORDINARY);
      expect(result).toEqual({ title: "x" });

      const sent = JSON.parse(readFileSync(seen, "utf8"));
      expect(sent.additionalProperties).toBe(false);
      expect(new Set(sent.required)).toEqual(
        new Set(["title", "summary", "score", "tag"]),
      );
      expect(sent.properties.tag.additionalProperties).toBe(false);
    } finally {
      cleanup();
    }
  });
});
