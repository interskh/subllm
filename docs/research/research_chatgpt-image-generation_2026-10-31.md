# Research: image generation through a ChatGPT subscription (2026-10-31)

Question: can subllm generate images through the ChatGPT subscription in two
modes — Codex and the chatgpt.com web UI — and can we reuse an existing project
instead of writing our own driver?

## Summary

| Mode | Fits the hard constraint? | Quota | Reuse | Status |
| --- | --- | --- | --- | --- |
| **Codex** (`codex exec` + built-in `image_gen`) | Yes — genuine official CLI | Codex usage | None needed; ~small adapter on `CodexLLM` | Live-verified on this machine |
| **Web** (real logged-in Chrome on chatgpt.com) | Grey — official client, but automated; against OpenAI ToS | ChatGPT chat quota (not Codex; works on free tier) | Subprocess to pinned `image-use --backend web` | Not live-verified; needs a go/no-go |

## Codex mode

Verified with `codex-cli 0.165.0` (`image_generation` feature: `stable`, on by
default):

```
codex exec --json --skip-git-repo-check --ephemeral "Generate an image: ... Use the image generation tool."
```

- Exit 0. The JSONL stream has `thread.started` (with `thread_id`), the agent
  reading the `imagegen` system skill, and a final `agent_message`. **No event
  carries the image or its path.**
- The PNG lands in `~/.codex/generated_images/<thread_id>/exec-<call_id>.png`.
  The system skill (`~/.codex/skills/.system/imagegen/SKILL.md`) documents this:
  built-in mode saves under `$CODEX_HOME/generated_images/...` and the tool has
  no destination-path argument; callers copy the file out.
- Collection recipe: read `thread_id` from `thread.started`, then glob
  `$CODEX_HOME/generated_images/<thread_id>/*.png` after the process exits.
  Zero files → treat as a refusal / tool-not-chosen error.
- Cost observed: ~91k input tokens (58k cached) for one tiny image, most of it
  the skill load plus the agent prompt. That is Codex quota per image, on top of
  the image itself.
- Model choice: the prompt must name the image tool; otherwise the agent may
  answer with SVG/HTML (the skill tells it to prefer code-native output for
  simple shapes).

### Not adopted: image-use's `codex` backend

`image-use --backend codex` does **not** run `codex`. It reads the OAuth token
from `~/.codex/auth.json`, POSTs directly to
`chatgpt.com/backend-api/codex/responses` with `tools: [image_generation]`, and
on expiry refreshes the token and **rewrites `auth.json`**. That is a
non-official client on an internal endpoint — exactly what the subllm hard
constraint excludes — and it mutates the official CLI's auth state.

## Web mode

### Why only a real browser works

Per image-use's technical doc, chatgpt.com gates sends behind three layers:
Cloudflare edge check, a sentinel proof-of-work, and a **Cloudflare Turnstile**
token. The first two are passable by a bare HTTP client; Turnstile is not, and
it is single-use. So there is no "capture token, then go headless" path. Every
working project drives a real, logged-in Chrome. A Playwright persistent
profile is the same approach with a different launcher. Reverse-engineered
`backend-api` clients (e.g. ChatGPTReversed, reverse-chatgpt, openai-sentinel)
are out: they break on frontend changes and are HTTP replays, not the client.

### Candidates

| Project | Shape | Signal | Verdict |
| --- | --- | --- | --- |
| **leeguooooo/image-use** (formerly chatgpt-imagegen) | Single-file stdlib Python CLI (~341 KB) + agent skill. Backends: `web` (default), `codex`, `gemini`, `agy`, `auto`. MIT. | 408★, pushed 2026-10-27, 182 commits, has tests incl. DOM fixtures for the 2026-09 redesign | **Recommended** for web mode (web backend only) |
| leeguooooo/chrome-use | Required by image-use web: CLI + MCP + Chrome extension + native-messaging host that drives your real Chrome. Apache-2.0. | 219★, pushed 2026-10-30 | Dependency; installs into your logged-in Chrome |
| liangming99/chatgpt-web-imagegen | Stdlib Python skill over a generic CDP proxy | 0★, 1 commit | Skip — unmaintained |
| sidhartha1s/claude-gpt-image-gen | Node Playwright CLI, persistent profiles | 0★, Windows Chrome path default | Skip — Node, Windows-oriented |

### How image-use's web backend works

From `docs/how-it-works.md` (read statically via `gh api`, not run):

1. `chrome-use open https://chatgpt.com/` — a regular chat (Temporary Chat
   disables the image tool).
2. Resolve or create an `imagegen` ChatGPT Project so runs stay out of the
   main history (`--project ""` opts out).
3. Paste the prompt via stdin, verify the exact editor text and uploads, click
   Send once, confirm a new message. Never re-sends on uncertainty.
4. Poll until streaming stops and a new `<img>` is stable across two reads.
5. Fetch the image bytes in-page with the browser's own cookies (the 2026-09
   redesign serves a `blob:` URL only the page can read), base64 → file.
6. Delete the conversation by default (`--keep-conversation` keeps it).

No tokens leave the browser. Rate-limit dialogs stop the run. `auto` will not
fall back after a prompt was sent (the image may still arrive in the chat).

### Integration option

Treat `image-use` as another official-looking CLI and drive it as a subprocess,
the same way subllm drives `codex`:

```
IMAGE_USE_NO_UPDATE_CHECK=1 image-use --backend web --quiet -o <out.png> "<prompt>"
```

- Pin a release; disable its self-update check (`IMAGE_USE_NO_UPDATE_CHECK=1`)
  and never call `image-use upgrade` from subllm.
- Only ever pass `--backend web` (never `codex`/`auto`, see above).
- Opt-in only; never selected by `FallbackLLM` by default.

Alternative: vendor the single file under MIT. Stable, but subllm then owns the
DOM-churn maintenance that the upstream's 182 commits represent. Not
recommended.

## Risks and open decisions

1. **ToS / account risk (web mode).** OpenAI's terms prohibit automated or
   programmatic extraction from its services. image-use's own disclaimer says
   "use at your own risk" and warns against powering a public service with a
   subscription; claude-gpt-image-gen states outright that it violates both
   providers' ToS and a ban is the worst case. subllm already accepts a related
   risk by tmux-driving `claude`, but here the account at risk is the ChatGPT
   account. Codex mode has no such issue.
2. **Untrusted-code install (web mode).** Live-verifying web mode requires
   installing chrome-use's extension and native-messaging host into the
   logged-in Chrome. Per the global untrusted-code policy, that needs explicit
   approval first. Not done.
3. **Fragility.** Web mode breaks on chatgpt.com redesigns (one already
   happened in 2026-09). Codex mode depends on the undocumented
   `generated_images/<thread_id>/` layout; pin it with a stub-binary test.
4. **Interface.** subllm's `base.py` is text-in/text-out. Image output needs a
   new method or result type (path list); design that in a spec, not here.

## Sources

- [leeguooooo/image-use](https://github.com/leeguooooo/chatgpt-imagegen) and its [how-it-works.md](https://github.com/leeguooooo/image-use/blob/main/docs/how-it-works.md)
- [leeguooooo/chrome-use](https://github.com/leeguooooo/chrome-use)
- [liangming99/chatgpt-web-imagegen](https://github.com/liangming99/chatgpt-web-imagegen)
- [sidhartha1s/claude-gpt-image-gen](https://github.com/sidhartha1s/claude-gpt-image-gen)
- [modbender chatgpt-image-generation skill](https://www.skillsdirectory.com/skills/modbender-chatgpt-image-generation)
- [web-api-playbook PR #33 — send needs sentinel + Turnstile, drives UI over CDP](https://github.com/Lee-SiHyeon/web-api-playbook/pull/33)
- [gin337/ChatGPTReversed](https://github.com/gin337/ChatGPTReversed), [s5treak/reverse-chatgpt](https://github.com/s5treak/reverse-chatgpt), [leetanshaj/openai-sentinel](https://github.com/leetanshaj/openai-sentinel) — HTTP-replay approaches, not adopted
- [imagegen-bridge-core](https://docs.rs/crate/imagegen-bridge-core/latest/source/README.md), [pi-codex-image-gen](https://pi.dev/packages/pi-codex-image-gen?name=imagegen) — community Codex image wrappers
- Local: `~/.codex/skills/.system/imagegen/SKILL.md`, `codex features list` (codex-cli 0.165.0)
