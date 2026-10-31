// Copy-button capture and text-completion rule after steipete/oracle (MIT).
(() => {
  const QUOTA_RE = /reached (our|the|your) (limit|usage cap)|usage limit|usage cap|rate limit|too many requests|limit of messages/i;
  const ERROR_RE = /^\s*(Unknown error|Something went wrong)/i;
  const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

  const rendered = (el) => {
    if (!el || !el.isConnected) return false;
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (cs.display === "none") return false;
      if (n === el && cs.visibility === "hidden") return false;
      if (cs.opacity === "0") return false;
    }
    if (getComputedStyle(el).display === "contents") return true;
    return el.getClientRects().length > 0;
  };
  const q = (root, sel) => Array.from(root.querySelectorAll(sel));
  const firstRendered = (root, sel) => q(root, sel).find(rendered) || null;
  const norm = (s) => String(s || "").replace(/\r\n?/g, "\n").replace(/ /g, " ")
    .split("\n").map((l) => l.trimEnd()).filter((l) => l.length > 0).join("\n");
  const keyOf = (t) => t.getAttribute("data-turn-key") || t.getAttribute("data-testid");

  const TURN_SEL = "[data-turn-key], [data-testid^='conversation-turn-']";
  const turns = () => q(document, TURN_SEL).filter((t) => !t.parentElement.closest(TURN_SEL));
  const findTurn = (key) => {
    const all = turns();
    return (key && all.find((t) => keyOf(t) === key)) || all[all.length - 1] || null;
  };

  const assistantScope = (turn) => {
    if (!turn) return null;
    const legacy = turn.matches("[data-message-author-role]") ? turn : turn.querySelector("[data-message-author-role]");
    if (legacy) {
      if (legacy.getAttribute("data-message-author-role") === "assistant") return legacy;
      for (let s = turn.nextElementSibling; s; s = s.nextElementSibling) {
        const a = s.querySelector("[data-message-author-role=assistant]");
        if (a) return a;
        if (s.querySelector("[data-message-author-role=user]")) break;
      }
      return null;
    }
    const unit = turn.querySelector("[data-content-search-unit-key$=':assistant']");
    if (!unit) return null;
    let el = unit;
    while (el.parentElement && el.parentElement !== turn && !el.parentElement.querySelector("[data-conversation-role=user]")) {
      el = el.parentElement;
    }
    return el;
  };

  const bodyOf = (scope) =>
    scope.querySelector("[data-markdown-text-style='assistant-message']") ||
    scope.querySelector(".markdown") || scope;
  const assistantText = (scope) => (scope ? (bodyOf(scope).innerText || "").trim() : "");

  const stopButton = () =>
    firstRendered(document, "button[aria-label='Stop'], button[aria-label='Stop streaming'], [data-testid='stop-button']");
  const copyButton = (scope) => {
    const turn = scope && scope.closest(TURN_SEL);
    if (!turn) return null;
    return q(turn, "button[aria-label='Copy'], [data-testid='copy-turn-action-button']")
      .find((b) => !b.closest("[data-content-search-unit-key$=':user'], [data-message-author-role=user]") && rendered(b)) || null;
  };

  const quotaDialog = () => {
    for (const el of q(document, "[role=dialog], [role=alertdialog], [role=alert], [data-testid*='banner']")) {
      if (!rendered(el)) continue;
      const text = (el.innerText || "").trim();
      if (QUOTA_RE.test(text)) return { kind: "quota", text: text.slice(0, 300) };
    }
    return null;
  };
  const errorText = (root) => {
    for (const el of q(root, "div, span, p, [role=alert]")) {
      const t = (el.innerText || "").trim();
      if (t.length > 0 && t.length < 200 && ERROR_RE.test(t) && rendered(el)) return t;
    }
    return null;
  };

  const composerEl = () =>
    document.querySelector("div.ProseMirror[role=textbox]") || document.querySelector("#prompt-textarea");
  const layoutOf = () =>
    document.querySelector("#prompt-textarea, [data-testid^='conversation-turn-']") ? "legacy" : "redesign";

  const state = () => {
    const composer = composerEl();
    const login = document.querySelector("[data-testid='login-button']") ||
      q(document, "button, a").find((b) => (b.textContent || "").trim() === "Log in");
    const challenge = !!document.querySelector(
      "iframe[src*='challenges.cloudflare.com'], #challenge-form, #cf-challenge-running") ||
      /^Just a moment/i.test(document.title);
    let mode = "unknown";
    const group = document.querySelector("[role=group][aria-label='Composer mode']");
    const pressed = group && q(group, "button[aria-pressed='true']")[0];
    const label = pressed && (pressed.textContent || "").trim();
    if (label === "Chat") mode = "chat";
    else if (label === "Work") mode = "work";
    else {
      const m = document.querySelector("[data-composer-mode]");
      const v = m && m.getAttribute("data-composer-mode");
      if (v === "chat" || v === "work") mode = v;
    }
    return {
      logged_in: !!composer && !login,
      challenge,
      mode_surface: mode,
      layout: layoutOf(),
      busy: !!stopButton(),
      quota: !!quotaDialog(),
    };
  };

  const composerMatches = (prompt) => {
    const ed = composerEl();
    if (!ed) return false;
    let got;
    if (ed.tagName === "TEXTAREA") got = ed.value;
    else {
      const ps = Array.from(ed.children).filter((c) => c.tagName === "P");
      got = ps.length ? ps.map((p) => p.textContent).join("\n") : ed.textContent;
    }
    return norm(got) === norm(prompt);
  };

  const acceptance = (url) => {
    const out = { state: "pending", turn_key: null, conversation_url: null };
    let u;
    try { u = new URL(url || location.href, location.href); } catch (e) { return out; }
    let path = u.pathname;
    try { path = decodeURIComponent(path); } catch (e) { /* keep raw */ }
    const m = path.match(/\/c\/([^/]+)\/?$/);
    if (m && UUID_RE.test(m[1])) {
      const all = turns();
      return {
        state: "accepted",
        turn_key: all.length ? keyOf(all[all.length - 1]) : null,
        conversation_url: u.origin + path,
      };
    }
    if (m && m[1].startsWith("local-chatgpt:") && errorText(document.body)) out.state = "rejected";
    return out;
  };

  const turnStatus = (turn_key, prev_text) => {
    const turn = findTurn(turn_key);
    const scope = assistantScope(turn);
    const text = assistantText(scope);
    const stop_visible = !!stopButton();
    const copy_visible = !!copyButton(scope);
    const text_stable = text.length > 0 && text === prev_text;
    let error_dialog = quotaDialog();
    if (!error_dialog && turn) {
      const t = errorText(turn);
      if (t) error_dialog = { kind: "error", text: t };
    }
    return {
      done: !stop_visible && copy_visible && text_stable,
      text,
      text_stable,
      stop_visible,
      copy_visible,
      images: scope ? q(scope, "img").filter(rendered).map((i) => i.src) : [],
      error_dialog,
    };
  };

  const captureText = async (turn_key) => {
    const scope = assistantScope(findTurn(turn_key));
    const fallback = () => ({ text: assistantText(scope), format: "plain" });
    const btn = copyButton(scope);
    if (!btn) return fallback();
    const hadClipboard = !!navigator.clipboard;
    if (!hadClipboard) Object.defineProperty(navigator, "clipboard", { value: {}, configurable: true });
    const cb = navigator.clipboard;
    const names = ["writeText", "write"];
    const saved = names.map((n) => Object.getOwnPropertyDescriptor(cb, n));
    let captured = "";
    const set = (n, fn) => Object.defineProperty(cb, n, { value: fn, configurable: true, writable: true });
    try {
      set("writeText", async (t) => { captured = String(t == null ? "" : t); });
      set("write", async (items) => {
        for (const item of items || []) {
          if (item.types && item.types.includes("text/plain")) {
            captured = await (await item.getType("text/plain")).text();
            return;
          }
        }
      });
      btn.click();
      for (let i = 0; i < 30 && !captured; i++) await new Promise((r) => setTimeout(r, 50));
    } finally {
      names.forEach((n, i) => {
        if (saved[i]) Object.defineProperty(cb, n, saved[i]);
        else delete cb[n];
      });
      if (!hadClipboard) delete navigator.clipboard;
    }
    return captured.trim() ? { text: captured, format: "markdown" } : fallback();
  };

  const servedModel = (turn_key) => {
    const scope = assistantScope(findTurn(turn_key));
    if (!scope) return null;
    const el = scope.matches("[data-message-model-slug]") ? scope : scope.querySelector("[data-message-model-slug]");
    return (el && el.getAttribute("data-message-model-slug")) || null;
  };

  return { state, composerMatches, acceptance, turnStatus, captureText, servedModel };
})()
