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
      expect(argv).toContain("-C "); // runs in a clean temp working dir (isolation)
      expect(argv).toContain("--ephemeral");
    } finally {
      cleanup();
    }
  });

  it("passes CODEX_HOME into the child environment", async () => {
    const homeLog = join(mkdtempSync(join(tmpdir(), "home-")), "home.txt");
    const cleanup = installFakeCodex(`#!/bin/bash
printf '%s' "$CODEX_HOME" > "${homeLog}"
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
printf 'ok' > "$out"
`);
    try {
      await runCodexExec("x", { codexHome: "/tmp/codex-clean-xyz" });
      expect(readFileSync(homeLog, "utf8")).toBe("/tmp/codex-clean-xyz");
    } finally {
      cleanup();
    }
  });

  it("places -- immediately before the prompt", async () => {
    const argvLog = join(mkdtempSync(join(tmpdir(), "dd-")), "dd.txt");
    const cleanup = installFakeCodex(`#!/bin/bash
echo "$@" > "${argvLog}"
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
printf 'ok' > "$out"
`);
    try {
      await runCodexExec("-leading-dash-prompt");
      // the prompt follows a literal `--` so a '-'-leading prompt isn't a flag
      expect(readFileSync(argvLog, "utf8")).toContain("-- -leading-dash-prompt");
    } finally {
      cleanup();
    }
  });

  it("maps rate-limit / quota / too-many-requests messages to QuotaError", async () => {
    for (const phrase of [
      "rate limit exceeded",
      "monthly quota reached",
      "too many requests",
    ]) {
      const cleanup = installFakeCodex(`#!/bin/bash
echo "${phrase}" >&2
exit 1
`);
      try {
        await expect(runCodexExec("x")).rejects.toBeInstanceOf(QuotaError);
      } finally {
        cleanup();
      }
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

  it("closes child stdin so codex does not hang waiting for piped input", async () => {
    // codex exec reads stdin even with an argv prompt. This stub blocks on `cat`
    // until stdin hits EOF; it only finishes if runCodexExec closed the child's stdin.
    const cleanup = installFakeCodex(`#!/bin/bash
cat > /dev/null            # read stdin to EOF; hangs forever if stdin stays open
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
printf 'stdin-was-closed' > "$out"
`);
    try {
      // short timeout: if stdin were left open, this would reject (timeout) instead of resolving.
      expect(await runCodexExec("x", { timeoutMs: 3000 })).toBe("stdin-was-closed");
    } finally {
      cleanup();
    }
  });
});
