import { describe, it, expect } from "vitest";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { CodexLLM } from "../src/codex.js";
import { ClientError, OutputError, RegionError } from "../src/errors.js";
import { RegionGuard } from "../src/preflight.js";
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

  it("rejects an uncompilable schema with ClientError", async () => {
    const cleanup = installFakeCodex(echoStub('{"topic":"ok"}'));
    // uppercase 'OBJECT'/'STRING' is the Gemini dialect, not valid JSON Schema;
    // ajv compile fails -> ClientError (a caller bug, not retried).
    const badSchema = {
      type: "OBJECT",
      properties: { topic: { type: "STRING" } },
    };
    try {
      await expect(
        new CodexLLM({ attempts: 1 }).completeJsonSchema("x", badSchema),
      ).rejects.toBeInstanceOf(ClientError);
    } finally {
      cleanup();
    }
  });

  it('emits model_reasoning_effort="medium" by default', async () => {
    const argvLog = join(mkdtempSync(join(tmpdir(), "eff-")), "eff.txt");
    const cleanup = installFakeCodex(`#!/bin/bash
echo "$@" > "${argvLog}"
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
printf 'ok' > "$out"
`);
    try {
      await new CodexLLM().complete("x");
      expect(readFileSync(argvLog, "utf8")).toContain(
        'model_reasoning_effort="medium"',
      );
    } finally {
      cleanup();
    }
  });

  it("retries a transient driver failure and succeeds on a later attempt", async () => {
    const dir = mkdtempSync(join(tmpdir(), "retry-"));
    const counter = join(dir, "n");
    const cleanup = installFakeCodex(`#!/bin/bash
cat > /dev/null
n=0; [ -f "${counter}" ] && n=$(cat "${counter}")
n=$((n+1)); echo "$n" > "${counter}"
if [ "$n" -lt 2 ]; then
  echo "transient boom" >&2
  exit 1
fi
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
printf 'recovered' > "$out"
`);
    try {
      const llm = new CodexLLM({ attempts: 3 });
      expect(await llm.complete("x")).toBe("recovered");
      expect(Number(readFileSync(counter, "utf8").trim())).toBe(2);
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

  it("regionGuard blocks before spawning codex and is not retried", async () => {
    // No fake codex installed: if the guard didn't short-circuit, run() would
    // spawn codex and fail differently. A counting lookup proves the hard stop
    // isn't retried despite attempts=3 (RegionError is not in the retry set).
    let lookups = 0;
    const guard = new RegionGuard({
      allowedRegions: ["US"],
      lookup: async () => {
        lookups += 1;
        return "CN";
      },
      ttlMs: 0,
    });
    const llm = new CodexLLM({ regionGuard: guard, attempts: 3 });
    await expect(llm.complete("x")).rejects.toBeInstanceOf(RegionError);
    expect(lookups).toBe(1);
  });
});
