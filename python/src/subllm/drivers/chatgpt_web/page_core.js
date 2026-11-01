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
  const MESSAGE_SEL = [
    "[data-content-search-unit-key$=':user']", "[data-message-author-role=user]",
    "[data-user-message-bubble]", "[data-conversation-role=user]",
    "[data-markdown-text-style='assistant-message']", ".markdown",
  ].join(", ");
  const errorText = (root) => {
    for (const el of q(root, "div, span, p, [role=alert]")) {
      if (el.closest(MESSAGE_SEL) || el.querySelector(MESSAGE_SEL)) continue;
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
      login_prompt: !!login,
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

  // Reference images: the hidden file inputs sit inside the composer form and their ids are
  // React-generated, so the chosen input gets a marker attribute and the substrate selects on that.
  const REF_MARK = "data-subllm-ref-input";
  const composerForm = () => {
    const c = composerEl();
    return c && c.closest("form");
  };
  const acceptTokens = (i) => (i.getAttribute("accept") || "").split(",").map((t) => t.trim()).filter(Boolean);
  const referenceInput = (count) => {
    const form = composerForm();
    if (!form) return null;
    for (const old of q(document, "[" + REF_MARK + "]")) old.removeAttribute(REF_MARK);
    // Camera capture inputs open the device camera, not a file picker.
    const cands = q(form, "input[type=file]").filter((i) =>
      !i.disabled && !i.hasAttribute("capture") && acceptTokens(i).includes("image/*") && (count <= 1 || i.multiple));
    if (!cands.length) return null;
    const rank = (i) => (i.multiple ? 0 : 100) + acceptTokens(i).length;
    cands.sort((a, b) => rank(a) - rank(b));
    cands[0].setAttribute(REF_MARK, "");
    return { selector: "input[" + REF_MARK + "]" };
  };
  const ATTACH_ERROR_RE = /fail|error|unsupported|too large|couldn.?t|unable|invalid/i;
  const attachments = () => {
    const form = composerForm();
    if (!form) return { count: 0, pending: 0, ready: 0, failed: 0 };
    const count = q(form, "button[aria-label^='Remove ']").length;
    const bars = q(form, "[role=progressbar][aria-label^='Uploading']").length;
    const blobs = q(form, "[data-composer-attachments] img").filter((i) => /^blob:/.test(i.getAttribute("src") || "")).length;
    const pending = Math.max(bars, blobs);
    const zone = form.querySelector("[data-composer-attachments]");
    const failed = zone ? q(zone, "[role=alert]").length + (ATTACH_ERROR_RE.test(zone.innerText || "") ? 1 : 0) : 0;
    // A chip is ready only on a positive signal: its preview has been swapped to a data: image.
    const loaded = zone ? q(zone, "img").filter((i) => /^data:image\//.test(i.getAttribute("src") || "")).length : 0;
    return { count, pending, ready: Math.max(0, Math.min(loaded, count - pending)), failed };
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

  const USER_SEL = "[data-content-search-unit-key$=':user'], [data-chatgpt-search-unit-key$=':user'], [data-message-author-role=user], [data-conversation-role=user], [data-user-message-bubble]";
  const turnImages = (turn, scope) => {
    const roots = turn ? [turn] : [];
    if (scope && !(turn && turn.contains(scope))) roots.push(scope);
    const out = [];
    for (const root of roots) {
      for (const i of q(root, "img")) {
        if (rendered(i) && i.complete && i.naturalWidth > 128 && !i.closest(USER_SEL) && !out.includes(i.src)) out.push(i.src);
      }
    }
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
      images: turnImages(turn, scope),
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

  // The current layout shows the served model only in the answer's "Try again" menu.
  const regenButton = (turn_key) => {
    const turn = findTurn(turn_key);
    return turn ? firstRendered(turn, "button[aria-label='Regenerate response']") : null;
  };
  const servedModelMenu = (turn_key) => {
    const b = regenButton(turn_key);
    if (!b || b.getAttribute("aria-haspopup") !== "menu" || !b.id) return null;
    return { selector: "#" + CSS.escape(b.id) };
  };
  const servedModel = (turn_key) => {
    const b = regenButton(turn_key);
    const menu = b && b.id && q(document, "[role=menu][data-state=open]").find((m) => m.getAttribute("aria-labelledby") === b.id);
    if (menu) {
      const item = q(menu, "[role=menuitem]").find((i) => /^\s*Try again/.test(i.textContent || ""));
      const m = item && (item.textContent || "").match(/•(.*)$/);
      const label = m && m[1].trim();
      if (label) return label;
    }
    const scope = assistantScope(findTurn(turn_key));
    if (!scope) return null;
    const el = scope.matches("[data-message-model-slug]") ? scope : scope.querySelector("[data-message-model-slug]");
    return (el && el.getAttribute("data-message-model-slug")) || null;
  };

  // The model picker is a step planner: it never dispatches events (Radix opens on real pointer input),
  // the substrate performs each returned action with real input and calls again.
  const LEVELS = ["Instant", "Medium", "High", "Extra High", "Pro"];
  const MODE_LABELS = { thinking: "Medium", pro: "Pro" }; // first thing to edit when the picker changes
  const PICKER_SEL = "button[aria-label='Select ChatGPT model']";
  const SLIDER_SEL = "[role=menu] [data-reasoning-slider]";
  const live = (el) => !el.closest("[inert], [aria-hidden=true]");
  // The label is the last text-bearing leaf: a placeholder ("Thinking effort") sits before it in the DOM,
  // and innerText runs the two together when layout does not break the line.
  const triggerLabel = (trigger) => {
    const leaves = q(trigger, "*").filter((e) => e.children.length === 0 && (e.textContent || "").trim());
    return leaves.length ? leaves[leaves.length - 1].textContent.trim() : "";
  };

  const selectMode = (target) => {
    const label = Object.prototype.hasOwnProperty.call(MODE_LABELS, target) ? MODE_LABELS[target]
      : LEVELS.includes(target) ? target : null;
    const out = (o) => ({ current: null, ...o });
    if (!label) return out({ action: "fail", detail: "unknown mode: " + target });
    const trigger = firstRendered(document, PICKER_SEL);
    if (!trigger) return out({ action: "fail", detail: "model picker not found" });
    const menu = q(document, "[role=menu][data-state=open]").find((m) => m.isConnected && live(m));
    if (menu) {
      const row = q(menu, "[data-reasoning-slider]").find(live);
      if (!row) return out({ action: "key", key: "Escape" });
      const status = q(menu, "[role=status]").find(live);
      const m = status && (status.textContent || "").trim().match(/^(.+), \d+ of \d+\.?$/);
      const current = m ? m[1] : null;
      if (!LEVELS.includes(current)) return out({ action: "fail", detail: "unknown picker level: " + current, current });
      if (current === label) return out({ action: "key", key: "Escape", current });
      if (row.querySelector("[data-locked='true']")) return out({ action: "fail", detail: "picker is locked", current });
      if (!(document.activeElement && document.activeElement.closest("[data-reasoning-slider]"))) {
        return out({ action: "focus", selector: SLIDER_SEL, current });
      }
      return out({ action: "key", key: LEVELS.indexOf(label) > LEVELS.indexOf(current) ? "ArrowRight" : "ArrowLeft", current });
    }
    const current = triggerLabel(trigger);
    if (!LEVELS.includes(current)) return out({ action: "fail", detail: "unknown picker level: " + current, current });
    if (current === label) return out({ action: "done", selected_label: current, current });
    return out({ action: "click", selector: PICKER_SEL, current });
  };

  const auditModel = (mode, served) => {
    if (!served) return "unverified";
    if (mode !== "pro") return "ok";
    return /\bpro\b/i.test(served) ? "ok" : "downgrade";
  };

  return { state, composerMatches, referenceInput, attachments, acceptance, turnStatus, captureText, servedModel, servedModelMenu, selectMode, auditModel };
})()
