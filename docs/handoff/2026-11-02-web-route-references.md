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

## Whole-run gate fix round 1

**What changed**
- A (privacy): the reference-exclusion fixture is trimmed to the `thread-scroll-container` subtree (sidebar/header with account and project ids gone). `sanitize.py` now rewrites `user-<16+ alnum>` and `g-p-<32 hex>` tokens and the value of any `data-*user-id|account-id|project-id` attribute. New `tests/test_fixture_privacy.py` scans every file under `tests/fixtures/` for those tokens, emails (the reserved `@example.invalid` placeholder is allowed), and absolute home paths. The non-synthetic-UUID rule applies to `chatgpt_web/` only: the recorded codex/claude CLI transcripts carry random per-run session UUIDs (not account data) and rewriting them is out of scope.
- B: the `attachments().count == 0` refusal now runs on every send (text and image, with or without references) on both substrates, after mode selection and before the composer is touched.
- C: readiness is positive: `ready` counts chips whose preview img src starts with `data:image/` (capped by `count - pending`); the attach loops need `count == n && pending == 0 && ready == n` on two reads. `resolve_references` (both routes) checks magic bytes (PNG, JPEG, WebP, GIF), so .txt/.pdf/0-byte files are a ClientError up front.
- D: README/contract wording fixed (stale-draft check happens after page load and mode selection; `failed` is heuristic and an unrecognized failure ends at the attach timeout; references need the redesign layout, legacy fails closed with "no image file input").
- E: `ImageGenerator.generate_image` gains keyword-only `references=()`. FallbackLLM and the TS side do not implement or forward it.
- Free live probe (ego lite, nothing typed/sent, space closed with `finish({keep: []})`): a synthetic 200px PNG attached via the marked input read pending for ~3.3 s, then `{count:1,pending:0,ready:1}`.

**Rejected**
- Applying the UUID rule to the CLI transcript fixtures (would rewrite recorded vendor output).
- Treating "no pending signal" as ready (the bug being fixed).

**Deferred**
- Attach deadline starts after `setInputFiles` (outer subprocess/watchdog budgets cover it).
- Duplicate references are not de-duplicated.
- The Remove-button chip count is form-wide, not scoped to the attachment zone.

**Evidence (gate round 1)**
- Mutations 6/6 caught (stale-draft check gated on references, ego and chrome; positive-ready signal dropped; `ready == want` dropped, ego and chrome; magic-byte check disabled). Files restored, checksums verified.
- Browser trio (real ego lite + Chrome, codex/claude tripwires only): 349 passed, tripwire log empty.
- Full hermetic suite (codex/claude/ego-browser tripwires first): 781 passed, 145 skipped, 1 failed. The failure was `test_codex_web.py::test_fixtures_are_sanitized` reading a stray `__pycache__` I had created in the fixtures dir; removed it, re-ran that file plus the privacy scan: 178 passed. Tripwire log empty.
- Deviations from the brief: UUID rule scoped to `chatgpt_web/` fixtures (the 10 codex/claude `.jsonl` transcripts carry per-run session UUIDs); email check allows the reserved `@example.invalid`.

## Whole-run gate fix round 2

- Chip count (`attachments()` in page_core.js) now counts a Remove button only if it sits inside `[data-composer-attachments]` OR its label ends in a file extension (`/\.[A-Za-z0-9]{1,5}$/`); not tied to images, so a non-image file chip is still caught. A "Remove tool" pill outside the zone counts 0, so text sends proceed. Core tests (run on ego + chrome): tool pill = 0, extension chip outside the zone = 1, extensionless chip inside the zone = 1.
- Sanitizer/test agreement: sanitize.py still emits `g-p-` + 32 zeros; the privacy scan now treats that all-zero id as synthetic (`g-p-(?!0{32})[0-9a-f]{32}`). New test runs the sanitizer on user/project tokens and asserts the output passes every scan check. The test execs sanitize.py source instead of importing it, so nothing writes a `__pycache__` into fixtures.
- Mutations: drop the zone clause -> caught; drop the whole filter (count every Remove) -> caught (2 tests x 2 substrates).
- Deferred: a pill that is BOTH inside the attachment zone and named "Remove x" would still count (never observed).
- Deferred (Codex re-check of round 2): on a layout without `[data-composer-attachments]` (legacy), a leftover attachment whose name has no extension (e.g. `README`) is not counted, so the stale-draft check would miss it. Every redesign capture has the zone; references already fail closed on the legacy layout.

## Run status

Whole-run gate: PASS after 2 fix cycles (bf4580b, 9779ae4); reviewer and Codex re-checks confirm every gate finding fixed. Full suite at 9779ae4: 783 passed, 147 skipped; browser 134 passed; tripwire log empty. Live: one `subllm image --route web --ref` call saved exactly one image that follows the reference (blue star from an orange-star reference). Privacy gate: the leaked sidebar ids in the first version of the reference fixture were rewritten out of branch history before merge.
