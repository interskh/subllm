# Run 2026-11-02-web-route-references

## T1 - driver layer: attach reference images in the chatgpt.com composer (ego + chrome)

**Decisions**
- Readiness lives in `page_core.js`: `referenceInput(n)` picks + marks the input, `attachments()` -> `{count, pending, ready, failed}`. Both substrates share it.
- Input choice: composer form's `input[type=file]`, accept token `image/*`, no `capture`, `multiple` required when n > 1; rank multiple first, then fewest accept tokens. One file also prefers the multiple input (the only single `image/*` input is the camera one, `capture=environment`, which would open a device camera). Marked with `data-subllm-ref-input`; selector `input[data-subllm-ref-input]` (ids are React-generated; ego needs a unique selector).
- Chip count = `button[aria-label^='Remove ']` in the form (NOT `rendered()`: the button is opacity-0 until hover). Pending = max("Uploading ..." progressbar count, chip `img` on `blob:`). Observed live: progressbar `role=progressbar aria-label="Uploading <name>"`, `blob:` -> `data:` on completion.
- Ready needs `count == n && pending == 0` on TWO consecutive reads (250 ms apart) to ride out a render gap between chip and indicator.
- Pre-check: a composer already holding attachments is a ClientError (a stale draft would be sent along with the references).
- Bound `60 + 15 * n` s (`ego.attach_timeout_s`), added to the ego subprocess budget and the chrome watchdog budget. Passed to the ego script as `ARGS.attach_timeout_ms`.
- Placement: after Chat/mode/post-mode-state checks, before focusing the composer; all failures raise before the keypress record -> plain ClientError.
- `references` accepts `str | Path`, must be absolute (`ego.reference_paths`, ClientError otherwise, before any lock/subprocess). Text path unchanged.
- `failed` is heuristic (an `[role=alert]` or error-ish text inside `[data-composer-attachments]`); the real error UI was never observed. Unrecognised failures still end as ClientError via the bound.
- sanitize.py: keeps `blob:`/`data:image` scheme (normalised to `blob:https://chatgpt.com/synthetic` / `data:image/<t>;base64,AAAA`) so pending-vs-ready fixtures stay distinguishable.

**Rejected**
- Selecting by input id / nth-of-type: ids are React-generated; the marker attribute is stable and unique.
- Falling back to the single `image/*` input for one file: it is the camera input.
- Trusting the Send button (enabled during upload) or only chip count (chip appears before upload ends).
- `rendered()` for chips/progress: opacity-0 remove button, and CSS-free fixtures.
- Polling the DOM for filenames: chip identity is by count only (names may be duplicates or sanitized).
- Removing chips from the composer on failure: the ego space is closed anyway, chrome page reloads; the pre-check covers leftovers.

**Assumptions**
- Layout: inputs and chips live inside `composerEl().closest("form")`; verified live 2026-11-02 (redesign layout only; legacy layout unprobed - check by running the fixture reads against a legacy capture).
- `setInputFiles` on a hidden input is allowed by ego lite (worked live; guide v2.2.0).
- Upload-error UI is unknown (see above).

**Deferred**
- Client wiring (`ChatGPTWebLLM.generate_image(references=...)`, CLI `--ref`, file validation) is T2.
- No live end-to-end send (spec forbids spending quota in this task); only composer-attach was probed live, nothing typed or sent.

**Touches**
- `drivers/chatgpt_web/{page_core.js,ego_run.js,ego.py,chrome.py}`; `docs/drivers-contract.md` (REFERENCE IMAGES, IMAGE BUDGET); `tests/test_chatgpt_web_{core,ego,chrome}.py`; fixtures `redesign-composer-attachment-{uploading,ready}.html`; `tests/fixtures/chatgpt_web/sanitize.py`.
- Shared surface: `run_image(..., references=)` on EgoDriver and ChromeDriver (T2 calls it); page_core exports `referenceInput`, `attachments`; `build_script(references=)`.

**Evidence**
- Full hermetic suite (tripwire codex/claude/ego-browser stubs first on PATH): 724 passed, 141 skipped; tripwire log empty. Browser tests (SUBLLM_BROWSER=1, codex/claude tripwires, real ego lite + headless Chrome): core + codex_web 269 passed; core 128 (64 per substrate, 11 new each); tripwire log empty.
- Mutation battery: 11/11 caught (pending, capture exclusion, multiple requirement, readyReads streak x2, count equality, failed check x2, attach call removed, budget, pre-check).
- Live capture: ego lite, synthetic solid-gradient PNG named reference-1.png, setInputFiles only, no key/click on Send; task space closed with `finish({ keep: [] })`. No login wall/challenge.

## T2 - client, CLI, docs, live proof

**Decisions**
- Shared helper `resolve_references(refs, *, forbid_comma=False)` in `_images.py` (readable regular file, `~` expanded, resolved absolute; OSError/RuntimeError -> ClientError). Codex calls it with `forbid_comma=True` (clap `-i` split is codex-only); web calls it plain, so a comma path is valid there.
- Web order: guard -> empty prompt -> references -> out_dir -> driver resolve. A bad reference touches no browser and creates no out_dir.
- Driver gets `references=` only when non-empty (empty = byte-identical old call; old FakeChrome-style drivers unaffected).
- CLI: dropped the web-route `--ref` exit-2 branch; `--ref` help/docstring updated.
- Removed the now-unused `import os` in codex.py (orphan from the refactor).

**Rejected**
- Duplicating the codex validation loop in the web client (drift risk).
- Forbidding ',' on the web route too: not a limitation there.

**Assumptions**
- Relative reference paths resolve against the caller's cwd (same as codex route).

**Deferred**
- The live-proof bug (reference thumbnail collected as an output) was fixed in the fix round below.

**Touches**
- `_images.py`, `codex.py`, `chatgpt_web.py`, `cli.py`; README, `docs/drivers-contract.md`, spec non-goal + deferred lines; tests `test_chatgpt_web_llm.py`, `test_cli_image.py` (web-route rejection test replaced).

**Evidence**
- Tripwire (codex/claude/ego-browser exit-127 stubs first on PATH) log empty for the focused, mutation and full runs. Mutations 8/8 caught.
- Live (1 call, ego lite, Chat quota): `subllm image --route web --ref <orange star png> -o <scratch>` exit 0, ~58 s, 2 PNGs 1448x1086. File 2 = blue star, same shape/position as reference (follows it). File 1 is byte-identical to the reference upload (the bug above). `text` was empty.

**Fix round (reference thumbnail collected as output)**
- DOM finding (redesign layout, shape only): both the user turn and the assistant turn sit under ONE `[data-turn-key]` wrapper. The user message unit carries `data-chatgpt-search-unit-key="<turn>:0:user"` (note `chatgpt-search`, not the `content-search` attribute USER_SEL knew); the thumbnail is `img` > `div[role=button][aria-label="User attachment"]` > unit. The generated image is `img` > `button[data-testid=generated-image-preview]` > gallery, outside any `:user` unit. No `data-message-author-role` in this layout.
- Fix: `USER_SEL` in page_core.js gains `[data-chatgpt-search-unit-key$=':user']` (structure, not filenames/aria text). Left `copyButton`/`MESSAGE_SEL` untouched (not needed for this bug).
- Rejected: matching `aria-label="User attachment"` (text, localisable); filtering by src/size (generated and reference can be equal-sized).
- Fixture `redesign-completed-image-reference.html` (sanitize.py output; ids synthetic, no URLs; images -> distinct 200px synthetic PNG data URLs, other imgs 1px). Test `test_image_probe_excludes_the_reference_attachment_in_the_user_turn` runs on ego + chrome.
- Mutation: removing the new selector fails the test on both substrates (2/2).
- Live re-run (1 call, fresh dir): exit 0, ~48 s, exactly one file, PNG 1448x1086, blue star, differs from the reference bytes.
- Browser tests (core/ego/chrome, codex+claude tripwires first on PATH, real ego + Chrome): 341 passed, tripwire log empty.
