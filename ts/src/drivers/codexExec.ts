/** Invoke `codex exec` (ChatGPT-subscription auth) as a clean text/JSON engine.
 *  Runs read-only and config-isolated to avoid the AGENTS.md context-pollution
 *  gotcha. The isolated subprocess boundary — see docs/drivers-contract.md. */
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { randomUUID } from "node:crypto";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { ClientError, OutputError, QuotaError } from "../errors.js";

const execFileP = promisify(execFile);

// stderr/stdout substrings that mean "out of subscription budget", not a bug.
// "try again later" is intentionally NOT here — too generic; it would
// misclassify auth/network failures as quota, and QuotaError must stay precise.
const QUOTA_RE = /usage limit|rate.?limit|quota|too many requests/i;

export interface CodexExecOptions {
  model?: string;
  reasoningEffort?: string;
  search?: boolean;
  codexHome?: string;
  schemaPath?: string;
  timeoutMs?: number;
}

/** Run one non-interactive codex turn; return the final message text.
 *  Raises QuotaError on a subscription-limit message, ClientError otherwise. */
export async function runCodexExec(
  prompt: string,
  opts: CodexExecOptions = {},
): Promise<string> {
  const {
    model,
    reasoningEffort,
    search = false,
    codexHome,
    schemaPath,
    timeoutMs = 120_000,
  } = opts;

  const outPath = join(tmpdir(), `subllm-codex-${randomUUID()}.txt`);
  // A fresh empty working dir so codex cannot load a project AGENTS.md from the
  // consumer's cwd. --ignore-user-config only skips $CODEX_HOME/config.toml; it
  // does NOT stop codex reading AGENTS.md from its working directory. -C points
  // codex at this clean dir; --ephemeral avoids persisting session files.
  const workDir = await mkdtemp(join(tmpdir(), "subllm-codex-work-"));
  const argv = [
    "exec",
    "-s",
    "read-only",
    "--skip-git-repo-check",
    "--ignore-user-config",
    "--ephemeral",
    "-C",
    workDir,
    "-o",
    outPath,
  ];
  if (model) argv.push("-m", model); // verbatim — never normalized
  if (reasoningEffort) {
    argv.push("-c", `model_reasoning_effort="${reasoningEffort}"`);
  }
  // NOTE: `--search` is NOT a valid codex exec flag; web search is a config override.
  if (search) argv.push("-c", `web_search="live"`);
  if (schemaPath) argv.push("--output-schema", schemaPath);
  argv.push("--", prompt); // -- so a prompt starting with '-' isn't parsed as a flag

  const env = { ...process.env };
  if (codexHome) env.CODEX_HOME = codexHome;

  try {
    try {
      const exec = execFileP("codex", argv, {
        env,
        cwd: workDir,
        timeout: timeoutMs,
        maxBuffer: 10 * 1024 * 1024,
      });
      exec.child.stdin?.end(); // codex exec reads stdin even with an argv prompt; close it so it doesn't hang
      await exec;
    } catch (e: unknown) {
      const err = e as NodeJS.ErrnoException & {
        stdout?: string;
        stderr?: string;
        killed?: boolean;
      };
      if (err.code === "ENOENT") {
        throw new ClientError("codex binary not found on PATH");
      }
      if (err.killed) {
        throw new ClientError(`codex exec was killed (timed out after ${timeoutMs}ms, or terminated by signal)`);
      }
      const blob = `${err.stderr ?? ""}\n${err.stdout ?? ""}`.trim();
      if (QUOTA_RE.test(blob)) {
        throw new QuotaError(`codex subscription limit: ${blob.slice(0, 200)}`);
      }
      throw new ClientError(
        `codex exec failed (exit ${String(err.code)}): ${blob.slice(0, 200)}`,
      );
    }
    try {
      return await readFile(outPath, "utf8");
    } catch (e) {
      // exit 0 but no readable -o file -> treat as empty output (retryable)
      throw new OutputError(
        `codex exec produced no readable output file: ${String(e)}`,
      );
    }
  } finally {
    await rm(outPath, { force: true });
    await rm(workDir, { recursive: true, force: true });
  }
}
