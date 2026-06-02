import { mkdtempSync, writeFileSync, chmodSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

/** Write a fake `codex` executable into a fresh temp dir and prepend that dir to
 *  PATH. Returns a cleanup fn that restores PATH and removes the dir. The child
 *  inherits process.env (incl. the prepended PATH) via runCodexExec. */
export function installFakeCodex(script: string): () => void {
  const dir = mkdtempSync(join(tmpdir(), "subllm-fakebin-"));
  const bin = join(dir, "codex");
  writeFileSync(bin, script);
  chmodSync(bin, 0o755);
  const prevPath = process.env.PATH ?? "";
  process.env.PATH = `${dir}:${prevPath}`;
  return () => {
    process.env.PATH = prevPath;
    rmSync(dir, { recursive: true, force: true });
  };
}

/** A stub that writes `text` to whatever path follows `-o`. */
export function echoStub(text: string): string {
  return `#!/bin/bash
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
printf '%s' '${text}' > "$out"
`;
}
