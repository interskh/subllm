# Codex review of the implementation plan — 2026-06-02

Reviewer: `codex exec` (gpt-5.5, read-only sandbox), grounded against the actual
local tools: `codex-cli 0.136.0`, `Claude Code 2.1.160`, real `~/.claude/projects`
paths, and ralph's live driver `~/Projects/ralph-loop/ralph_lib/drivers.py`.

Verdict (original): **Not executable as written.** All findings below were applied
to `docs/plan.md` (commit following this file).

## CRITICAL — applied

1. `--search` is not a valid `codex exec` flag in 0.136.0 (verified: "unexpected
   argument '--search'"). → use `-c web_search="live"`. *(Task 5)*
2. tmux init command must be ONE shell string, not split trailing argv. → added
   `_build_claude_cmd` (shlex-style quoting), passed as a single arg. *(Task 8)*
3. `CLAUDE_CONFIG_DIR` was never passed into the tmux/claude process → transcript
   written elsewhere, driver times out. → `_env_args` passes it via `-e`. *(Task 8)*
4. Transcript path encoding wrong (`replace("/","-")`). → ralph's
   `re.sub(r"[^A-Za-z0-9-]","-", str(Path(work_dir).resolve()))`, plus `--session-id`
   for a deterministic `<uuid>.jsonl` filename (no newest-file globbing). *(Task 8)*
5. `_wait_for_ready` trusted any pane output. → `_wait_for_banner` matches the
   real prompt markers (`❯`, `│ >`) and handles the first-run trust dialog. *(Task 8)*

## SHOULD-FIX — applied

- `send-keys -l` unsafe for multiline / >4KB prompts → `load-buffer`/`paste-buffer`
  with paste-placeholder wait (summaries routinely exceed 4KB). *(Task 8)*
- Completion required an `end_turn` event WITH text (thinking-only `end_turn`
  no longer ends the turn early). *(Task 8)*
- Tools disabled by default (`--tools ''`) so unattended turns don't deadlock on
  approval. *(Task 8 / 9)*
- `CodexLLM.complete_json_schema` now validates with the Pydantic model. *(Task 6)*
- `ClaudeLLM` now implements `complete_json_schema` (instruct + validate). *(Task 9)*
- Greedy `\{.*\}` → `json.JSONDecoder().raw_decode` scan, fence-aware. *(Task 9)*
- CLI test fixed: `parse_args` moved inside the try (argparse `choices` raises
  `SystemExit` before the old try block). *(Task 7)*
- Faithful fake `tmux`/`claude` smoke test (asserts single command string,
  `CLAUDE_CONFIG_DIR` passthrough, correct path encoding). *(Task 8)*

## Validated as correct (no change needed)

`-o`, `--output-schema`, `-c model_reasoning_effort`, `--` before prompt,
`CODEX_HOME` isolation, `--ignore-user-config` (still reads auth from CODEX_HOME),
and `bypassPermissions` are all real/correct.

## NICE-TO-HAVE — not applied (tracked, out of chosen scope)

- Quota mapping now inspects stderr+stdout (this one WAS applied). *(Task 5)*
- Auto-create isolated `CODEX_HOME` + copy `auth.json`, or fail early if missing.
- `--ignore-rules` / `--ephemeral` to fully match the research isolation recipe.
- `RegionGuard`: validate `ttl_s >= 0`.
