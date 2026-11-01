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
