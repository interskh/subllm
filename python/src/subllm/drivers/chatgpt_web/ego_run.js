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
let outcome = "error";
let conversationUrl = null;
try {
  task = await taskSpace(ARGS.space);
  const page = task.page("p1");
  const core = (name, ...args) =>
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
    if (st.logged_in || st.challenge) break;
    await sleep(500);
  }
  if (!st) stop("client", "page state unavailable");
  if (st.challenge) stop("human_action", "Cloudflare challenge");
  if (!st.logged_in) stop("human_action", "log in to chatgpt.com in ego lite");
  if (st.quota) stop("quota", "usage limit reached");
  if (st.mode_surface !== "chat") {
    stop("client", st.mode_surface === "work" ? "composer is in Work mode" : "Chat mode not confirmed");
  }
  if (st.busy) stop("client", "page is busy before the send");
  if (await isChat()) stop("client", "not a new chat");

  const hasPm = await page.evaluate("!!document.querySelector('div.ProseMirror[role=textbox]')");
  await page.click(hasPm ? "div.ProseMirror[role=textbox]" : "#prompt-textarea", { label: "focus composer" });
  const parts = ARGS.prompt.split("\n");
  for (let i = 0; i < parts.length; i++) {
    if (parts[i]) await page.keyboard.type(parts[i], { delay: 40 });
    if (i < parts.length - 1) await page.keyboard.press("Shift+Enter");
  }
  await sleep(400);
  const record = () => {
    fs.writeFileSync(KEYPRESS_FILE, "keypress");
    event({ phase: "keypress" });
  };
  if (await isChat()) {
    record();
    stop("client", "chat opened before the send key; a send may have happened");
  }
  if (!(await core("composerMatches", ARGS.prompt))) stop("client", "composer text does not match the prompt");

  record();
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

  let prev = null;
  let ts = null;
  for (;;) {
    ts = await core("turnStatus", acc.turn_key, prev);
    if (ts.error_dialog) {
      const e = ts.error_dialog;
      stop(e.kind === "quota" ? "quota" : "client", e.text || "error dialog");
    }
    if (ts.done) break;
    if (Date.now() >= answerDeadline) stop("client", "timed out waiting for the answer");
    prev = ts.text;
    await sleep(700);
  }

  const cap = await core("captureText", acc.turn_key);
  if (!cap || !String(cap.text || "").trim()) stop("output", "empty answer");
  const served = await core("servedModel", acc.turn_key);
  outcome = "ok";
  result({
    ok: true,
    text: cap.text,
    format: cap.format === "markdown" ? "markdown" : "plain",
    images: [],
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
  try {
    if (task) {
      if (outcome === "human_action") await task.handOff();
      else await task.finish({ keep: [] });
    }
  } catch (e) {
    console.log("cleanup failed: " + String((e && e.message) || e));
  }
}
