// Body of the generated ego script. ego.py prepends const ARGS, KEYPRESS_FILE, CORE.
const fs = await import("node:fs");
let answerDeadline = Date.now() + ARGS.timeout_ms;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const event = (o) => console.log("SUBLLM_EVENT " + JSON.stringify(o));
let resultPrinted = false;
const result = (o) => {
  if (resultPrinted) return;
  resultPrinted = true;
  console.log("SUBLLM_RESULT " + JSON.stringify(o));
  if (!o.ok) process.exitCode = 1;
};
class Stop extends Error {
  constructor(kind, detail, extra) {
    super(detail);
    this.kind = kind;
    this.extra = extra || {};
  }
}
const stop = (kind, detail, extra) => { throw new Stop(kind, detail, extra); };

let task = null;
let page = null;
let core = null;
let outcome = "error";
let conversationUrl = null;
let restoreTo = null;
let pickerAbort = false;
const RESTORE_BOUND_MS = 15000;
const MODE = ARGS.mode || "default";

// Drives the page_core selectMode planner with real input: click/focus/key, bounded.
const runPicker = async (target, track) => {
  let first = null;
  for (let i = 0; i < 16; i++) {
    if (pickerAbort) return { ok: false, first, detail: "restore timed out" };
    const s = await core("selectMode", target);
    if (pickerAbort) return { ok: false, first, detail: "restore timed out" };
    if (first === null) {
      first = s.current;
      // Recorded before any action so a throw mid-loop still restores the level.
      if (track && first && s.action !== "done") restoreTo = first;
    }
    if (s.action === "done") return { ok: true, first, label: s.selected_label };
    if (s.action === "fail") return { ok: false, first, detail: s.detail };
    if (s.action === "click") await page.click(s.selector, { label: "open model picker" });
    else if (s.action === "focus") await page.focus(s.selector);
    else if (s.action === "key") await page.keyboard.press(s.key);
    else return { ok: false, first, detail: "unknown picker action" };
    await sleep(350);
  }
  return { ok: false, first, detail: null };
};

try {
  task = await taskSpace(ARGS.space);
  page = task.page("p1");
  core = (name, ...args) =>
    page.evaluate("(" + CORE + ")." + name + "(" + args.map((a) => JSON.stringify(a ?? null)).join(",") + ")");
  const isChat = async () => /\/c\//.test(new URL(await page.url()).pathname);

  await page.cdp("Page.bringToFront");
  // chatgpt.com can stay "interactive" past goto's load wait; the state loop below is the real wait.
  try {
    await page.goto("https://chatgpt.com/", { waitUntil: "domcontentloaded" });
  } catch (e) {
    if (!/navigation committed/.test(String((e && e.message) || e))) throw e;
  }

  let st = null;
  const waitEnd = Date.now() + 45000;
  while (Date.now() < waitEnd) {
    st = await core("state");
    // A login control can flash before the session hydrates, so it only classifies after the wait.
    if (st.logged_in || st.challenge) break;
    await sleep(500);
  }
  if (!st) stop("client", "page state unavailable");
  if (st.challenge) stop("human_action", "Cloudflare challenge");
  if (!st.logged_in) {
    if (st.login_prompt) stop("human_action", "log in to chatgpt.com in ego lite");
    stop("client", "chatgpt.com did not become ready");
  }
  if (st.quota) stop("quota", "usage limit reached");
  if (st.mode_surface !== "chat") {
    stop("client", st.mode_surface === "work" ? "composer is in Work mode" : "Chat mode not confirmed");
  }
  if (st.busy) stop("client", "page is busy before the send");
  if (await isChat()) stop("client", "not a new chat");

  if (MODE !== "default") {
    const sel = await runPicker(MODE, true);
    if (sel.ok && sel.first === sel.label) restoreTo = null;
    if (!sel.ok) stop("client", sel.detail ? "mode selection failed: " + sel.detail : "mode selection unconfirmed");
    const after = await core("state");
    if (after.challenge) stop("human_action", "Cloudflare challenge");
    if (!after.logged_in && after.login_prompt) stop("human_action", "log in to chatgpt.com in ego lite");
    if (after.quota) stop("quota", "usage limit reached");
    if (!after.logged_in || after.mode_surface !== "chat" || after.busy) {
      stop("client", "Chat mode not confirmed after mode selection");
    }
  }

  const hasPm = await page.evaluate("!!document.querySelector('div.ProseMirror[role=textbox]')");
  await page.click(hasPm ? "div.ProseMirror[role=textbox]" : "#prompt-textarea", { label: "focus composer" });
  // Any key event can send early, so the durable record comes before the first one.
  const record = () => {
    fs.writeFileSync(KEYPRESS_FILE, "keypress");
    event({ phase: "keypress" });
  };
  record();
  const parts = ARGS.prompt.split("\n");
  for (let i = 0; i < parts.length; i++) {
    if (parts[i]) await page.keyboard.type(parts[i], { delay: 40 });
    if (i < parts.length - 1) await page.keyboard.press("Shift+Enter");
  }
  await sleep(400);
  if (await isChat()) {
    stop("client", "chat opened before the send key; a send may have happened");
  }
  if (!(await core("composerMatches", ARGS.prompt))) stop("client", "composer text does not match the prompt");

  await page.cdp("Page.bringToFront");
  await page.keyboard.press("Enter");
  answerDeadline = Date.now() + ARGS.timeout_ms;

  const accEnd = Date.now() + 30000;
  let acc = { state: "pending" };
  while (Date.now() < accEnd) {
    acc = await core("acceptance");
    if (acc.state !== "pending") break;
    await sleep(250);
  }
  if (acc.state === "rejected") {
    event({ phase: "rejected" });
    stop("client", "send rejected before the server accepted it");
  }
  if (acc.state !== "accepted") stop("client", "send not confirmed");
  conversationUrl = acc.conversation_url;
  event({ phase: "accepted", conversation_url: conversationUrl });

  const image = ARGS.kind === "image";
  let prev = null;
  let prevImages = null;
  let noImageSince = null;
  let ts = null;
  for (;;) {
    ts = await core("turnStatus", acc.turn_key, prev);
    if (ts.error_dialog) {
      const e = ts.error_dialog;
      stop(e.kind === "quota" ? "quota" : "client", e.text || "error dialog");
    }
    if (image) {
      if (!ts.stop_visible && ts.images.length > 0 && JSON.stringify(ts.images) === JSON.stringify(prevImages)) break;
      if (ts.done && ts.images.length === 0) {
        if (noImageSince === null) noImageSince = Date.now();
        if (Date.now() - noImageSince >= 8000) stop("output", "no image in the answer");
      } else {
        noImageSince = null;
      }
      prevImages = ts.images;
    } else if (ts.done) {
      break;
    }
    if (Date.now() >= answerDeadline) stop("client", "timed out waiting for the answer");
    prev = ts.text;
    await sleep(700);
  }

  const cap = await core("captureText", acc.turn_key);
  if (!image && (!cap || !String(cap.text || "").trim())) stop("output", "empty answer");
  let served = null;
  let menu = null;
  if (MODE !== "default") {
    try { menu = await core("servedModelMenu", acc.turn_key); } catch (e) { menu = null; }
  }
  if (menu) {
    try {
      await page.click(menu.selector, { label: "open the answer's Try again menu" });
      await sleep(600);
      served = await core("servedModel", acc.turn_key);
    } catch (e) {
      served = null;
    } finally {
      try { await page.keyboard.press("Escape"); await sleep(300); } catch (e) { /* menu close is best effort */ }
    }
  } else {
    served = await core("servedModel", acc.turn_key);
  }
  if (MODE !== "default" && (await core("auditModel", MODE, served)) === "downgrade") {
    stop("downgrade", "requested " + MODE + ", served " + served, {
      text: (cap && cap.text) || "",
      served_model: served,
      requested_mode: MODE,
    });
  }
  const images = [];
  if (image) {
    for (const [i, src] of ts.images.entries()) {
      const path = ARGS.image_dir + "/img-" + (i + 1);
      const r = await page.fetch(src, { saveAs: path, timeout: 30000 });
      if (!r || !r.ok) stop("client", "image fetch failed: " + ((r && (r.status || r.statusText)) || "no response"));
      images.push(path);
    }
  }
  outcome = "ok";
  result({
    ok: true,
    text: (cap && cap.text) || "",
    format: cap && cap.format === "markdown" ? "markdown" : "plain",
    images,
    served_model: served,
    conversation_url: conversationUrl,
  });
} catch (e) {
  if (e instanceof Stop) {
    outcome = e.kind;
    result({
      ok: false,
      kind: e.kind,
      detail: e.message,
      conversation_url: conversationUrl,
      text: null,
      served_model: null,
      ...e.extra,
    });
  } else {
    result({
      ok: false,
      kind: "client",
      detail: String((e && e.message) || e),
      conversation_url: conversationUrl,
      text: null,
      served_model: null,
    });
  }
} finally {
  if (restoreTo && outcome !== "human_action" && page) {
    let timer = null;
    try {
      const bound = new Promise((r) => {
        timer = setTimeout(() => { pickerAbort = true; r({ ok: false, detail: "picker restore timed out" }); }, RESTORE_BOUND_MS);
      });
      const back = await Promise.race([runPicker(restoreTo, false), bound]);
      if (!back.ok) console.log("picker restore failed: " + (back.detail || "unconfirmed"));
    } catch (e) {
      console.log("picker restore failed: " + String((e && e.message) || e));
    } finally {
      clearTimeout(timer);
    }
  }
  try {
    if (task) {
      if (outcome === "human_action") await task.handOff();
      else await task.finish({ keep: [] });
    }
  } catch (e) {
    console.log("cleanup failed: " + String((e && e.message) || e));
  }
}
