# Drivers contract

The `subllm/drivers/` package is the only language-specific boundary. A TS/Go
port reimplements just these two contracts; the rest of subllm (BaseLLM shape,
fallback, region guard) is trivial to re-express in any language.

## codex_exec

- Invoke: `codex exec -s read-only --skip-git-repo-check --ignore-user-config
  -o <tmpfile> [-m MODEL] [-c model_reasoning_effort="EFFORT"]
  [-c web_search="live"] [--output-schema FILE] -- PROMPT`
- NOTE: `--search` is NOT a valid `codex exec` flag (verified on codex-cli
  0.136.0: "unexpected argument '--search'"). Enable web search for exec via the
  config override `-c web_search="live"` instead.
- Auth: ChatGPT subscription login (no API key). Set `CODEX_HOME` to an isolated
  dir containing only `auth.json` to avoid AGENTS.md context pollution.
  `--ignore-user-config` still reads auth from `CODEX_HOME`.
- Output: the final message is written to `<tmpfile>` (the `-o` path); read it.
- Errors: non-zero exit with stderr+stdout matching
  `/usage limit|rate.?limit|quota|too many requests/i` => QuotaError; missing
  binary / other non-zero exit / timeout => ClientError.
  (NOTE: "try again later" is intentionally excluded — too generic; would
  misclassify auth/network failures as quota.)

## claude_tmux

- Build the claude command as ONE shell string (tmux runs it via `sh -c`; split
  trailing argv after the window flags is mis-parsed):
  `claude --session-id <uuid> --permission-mode bypassPermissions [--model M] --tools ''`.
  `--tools ''` disables tools so an unattended turn never deadlocks on approval.
- Spawn: `tmux new-session -d -s <name> -x 200 -y 50 -c <workdir>
  -e CLAUDE_CONFIG_DIR=<cfg> [-e PATH=… -e HOME=… -e USER=… -e LANG=… -e TERM=…]
  '<claude-cmd>'`. The full env-passthrough set is
  `{CLAUDE_CONFIG_DIR, PATH, HOME, USER, LANG, TERM}`.
  Passing `CLAUDE_CONFIG_DIR` via `-e` is REQUIRED — otherwise claude writes its
  transcript somewhere the reader doesn't look and the turn times out.
- Auth: regular Claude subscription (interactive path — NOT `claude -p`, which
  after 2026-06-15 bills a separate programmatic credit).
- Readiness: poll `capture-pane` for the input-prompt markers (`❯`, `│ >`). On
  first run in a project a workspace-trust dialog ("Do you trust the files in
  this folder") appears first — accept it (send `1`, then `Enter`) and keep
  polling.
- Send prompt: for single-line ≤4KB, `send-keys -t <name> -l "<prompt>"` then a
  separate `send-keys -t <name> Enter`. For multiline or >4KB, `load-buffer` +
  `paste-buffer`, wait for the `[Pasted text #N]` placeholder, then `Enter`.
- Detect completion: read the DETERMINISTIC transcript
  `<cfg>/projects/<encoded>/<uuid>.jsonl`, where
  `encoded = re.sub(r"[^A-Za-z0-9-]", "-", str(Path(workdir).resolve()))` and
  `<uuid>` is the `--session-id`. A turn is complete when an
  `{"type":"assistant"}` record has `message.stop_reason == "end_turn"` AND
  contains a text content block. IGNORE partial streaming lines (stop_reason
  null) and thinking-only `end_turn` events. Final text = concatenation of that
  record's text blocks.
- Always `tmux kill-session -t <name>` in a finally block.
- Errors: limit banner in pane (matches any of: "usage limit", "rate limit",
  "out of credit") => QuotaError; tmux spawn failure (new-session non-zero exit)
  / missing tmux binary / timeout => ClientError; no completed text turn =>
  OutputError.
