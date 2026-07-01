// ChatGPT Git-Drop Bridge: content script.
//
// Sends prompts into ChatGPT. It never reads the assistant response as the
// result; completion is controlled by the server's git-drop commit polling.
// Monitors GitHub connector health via DOM indicators and network probe events.

(function () {
  "use strict";

  const VERSION = "1.2.0";
  const CFG = { channel: "" };
  const ROTATE_AFTER_TURNS = 20;  // fresh-start after this many user messages
  let activated = false;
  let busy = false;
  let currentTask = "";
  let turnCount = 0;

  // ── Connector health state ──
  let probeNonce = "";
  const connectorState = {
    state: "unknown",           // connected|disconnected|initializing|stuck|unknown
    lastTransition: null,       // ISO timestamp of last state change
    stuckSince: null,           // when "Preparing app tools" first seen
    networkEvents: [],          // recent probe events (capped at 50)
  };
  const CONNECTOR_HEALTH_INTERVAL = 60000;  // 60s between health reports
  const CONNECTOR_EVENT_CAP = 50;

  const SEL = {
    input: [
      "#prompt-textarea",
      'div[contenteditable="true"][data-placeholder]',
      "div.ProseMirror[contenteditable]",
      'textarea[data-id="root"]',
      '[contenteditable="true"]'
    ],
    send: [
      '[data-testid="send-button"]',
      '[data-testid="composer-send-button"]',
      'button[aria-label="Send prompt"]',
      'button[aria-label="Send"]',
      'button[aria-label="Send message"]',
      'form button[type="submit"]'
    ],
    stop: [
      '[data-testid="stop-button"]',
      'button[aria-label="Stop generating"]',
      'button[aria-label="Stop"]',
      'button[aria-label="Stop streaming"]'
    ]
  };

  function log(...args) {
    console.log("[ask-gpt-git]", ...args);
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function first(selectors) {
    for (const selector of selectors) {
      const el = document.querySelector(selector);
      if (el) return el;
    }
    return null;
  }

  function tabId() {
    let id = sessionStorage.getItem("ask-gpt-git-tab-id");
    if (!id) {
      id = `tab-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
      sessionStorage.setItem("ask-gpt-git-tab-id", id);
    }
    return id;
  }

  function channelKey() {
    return `ask-gpt-git-channel:${tabId()}`;
  }

  function stableStorageKey() {
    const path = location.pathname.replace(/\/$/, "");
    const conv = path.match(/\/c\/([^/?#]+)/);
    if (conv && conv[1]) return `ask-gpt-git-conversation:${conv[1]}`;
    return "";
  }

  function saveChannel(channel) {
    if (channel) {
      localStorage.setItem(channelKey(), channel);
      const stable = stableStorageKey();
      if (stable) chrome.storage.local.set({ [stable]: channel });
    } else {
      localStorage.removeItem(channelKey());
      const stable = stableStorageKey();
      if (stable) chrome.storage.local.remove(stable);
    }
  }

  // ── Bridge API via background service worker ──

  function api(method, path, body) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage(
        { type: "bridge", method, path, body },
        (resp) => {
          if (chrome.runtime.lastError) {
            reject(new Error(chrome.runtime.lastError.message));
          } else if (resp && resp.ok) {
            resolve(resp.data);
          } else {
            reject(new Error(resp?.error || "unknown error"));
          }
        }
      );
    });
  }

  function registerTab() {
    if (!activated || !CFG.channel) return;
    chrome.runtime.sendMessage({
      type: "tab-register",
      channel: CFG.channel,
      busy,
      currentTask
    }, () => void chrome.runtime.lastError);
  }

  function reportState() {
    if (!activated || !CFG.channel) return;
    chrome.runtime.sendMessage({
      type: "tab-state",
      channel: CFG.channel,
      busy,
      currentTask
    }, () => void chrome.runtime.lastError);
  }

  function setBusy(nextBusy, taskId) {
    busy = !!nextBusy;
    currentTask = taskId || "";
    updateBadge();
    reportState();
  }

  function inputText(el) {
    if (!el) return "";
    if (el.tagName === "TEXTAREA") return el.value || "";
    return el.innerText || el.textContent || "";
  }

  function sendButton() {
    const btn = first(SEL.send);
    if (!btn) return null;
    if (btn.disabled) return null;
    if (btn.getAttribute("aria-disabled") === "true") return null;
    return btn;
  }

  function isGenerating() {
    return !!first(SEL.stop);
  }

  async function waitForComposerIdle(timeoutMs) {
    const start = Date.now();
    while (isGenerating()) {
      if (Date.now() - start > timeoutMs) return false;
      await sleep(1000);
    }
    return true;
  }

  async function waitForInput(timeoutMs) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const el = first(SEL.input);
      if (el) return el;
      await sleep(250);
    }
    return null;
  }

  async function injectPrompt(text) {
    const el = await waitForInput(30000);
    if (!el) throw new Error("composer not found");
    el.focus();

    if (el.tagName === "TEXTAREA") {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
      setter.call(el, text);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      return true;
    }

    // Method 1: ClipboardEvent paste (works with ProseMirror).
    try {
      el.textContent = "";
      el.dispatchEvent(new Event("input", { bubbles: true }));
      const dt = new DataTransfer();
      dt.setData("text/plain", text);
      el.dispatchEvent(new ClipboardEvent("paste", {
        clipboardData: dt,
        bubbles: true,
        cancelable: true
      }));
      await sleep(100);
      if (inputText(el).trim()) return true;
    } catch (err) {
      log("clipboard injection failed", err.message);
    }

    // Method 2: execCommand insertText.
    try {
      document.execCommand("selectAll");
      document.execCommand("delete");
      if (document.execCommand("insertText", false, text)) {
        await sleep(100);
        if (inputText(el).trim()) return true;
      }
    } catch (err) {
      log("execCommand injection failed", err.message);
    }

    // Method 3: Direct DOM write + InputEvent.
    el.innerHTML = "";
    const p = document.createElement("p");
    p.textContent = text;
    el.appendChild(p);
    el.dispatchEvent(new InputEvent("input", {
      inputType: "insertText",
      data: text,
      bubbles: true
    }));
    await sleep(100);
    return !!inputText(el).trim();
  }

  function fullClick(el) {
    const rect = el.getBoundingClientRect();
    const init = {
      clientX: rect.left + rect.width / 2,
      clientY: rect.top + rect.height / 2,
      bubbles: true,
      cancelable: true,
      view: window
    };
    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
      const EventType = type.startsWith("pointer") && window.PointerEvent ? PointerEvent : MouseEvent;
      el.dispatchEvent(new EventType(type, init));
    }
  }

  function fireEnter(el) {
    el.focus();
    for (const type of ["keydown", "keypress", "keyup"]) {
      el.dispatchEvent(new KeyboardEvent(type, {
        key: "Enter",
        code: "Enter",
        keyCode: 13,
        which: 13,
        bubbles: true,
        cancelable: true
      }));
    }
  }

  function sendTookEffect(prevUserCount) {
    if (isGenerating()) return true;
    const el = first(SEL.input);
    const empty = !el || !inputText(el).trim();
    const userCount = document.querySelectorAll('[data-message-author-role="user"]').length;
    return empty && userCount > prevUserCount;
  }

  async function clickSend() {
    const prevUserCount = document.querySelectorAll('[data-message-author-role="user"]').length;
    await sleep(500);

    for (let attempt = 0; attempt < 3; attempt++) {
      const btn = sendButton();
      if (btn) {
        fullClick(btn);
      } else {
        const el = first(SEL.input);
        if (!el) return false;
        fireEnter(el);
      }

      for (let i = 0; i < 12; i++) {
        await sleep(250);
        if (sendTookEffect(prevUserCount)) return true;
      }
    }
    return false;
  }

  // ── Conversation rotation (fresh-start) ──

  function countUserMessages() {
    try {
      // ChatGPT renders user messages with data-message-author-role="user"
      const msgs = document.querySelectorAll('[data-message-author-role="user"]');
      return msgs.length;
    } catch (_) { return 0; }
  }

  let _freshStarting = false;
  async function doFreshStart(reason) {
    if (_freshStarting) return;
    _freshStarting = true;
    log("fresh-start:", reason);

    try {
      // Try clicking New Chat button
      const selectors = [
        'a[href="/"]',
        '[data-testid="create-new-chat-button"]',
        'nav a[href="/"]',
        'button[aria-label="New chat"]',
        'a[aria-label="New chat"]',
      ];
      let btn = null;
      for (const s of selectors) {
        btn = document.querySelector(s);
        if (btn) break;
      }

      if (btn) {
        btn.click();
        log("fresh-start: clicked New Chat");
      } else {
        log("fresh-start: no button found, navigating to /");
        window.location.href = "https://chatgpt.com/";
      }

      turnCount = 0;
      addNetworkEvent("fresh_start", { reason });
      await new Promise(r => setTimeout(r, 4000));
    } finally {
      _freshStarting = false;
    }
  }

  // ── Task processing ──

  async function processTask(task) {
    if (!activated || !CFG.channel) return { ok: false, error: "tab is not activated" };
    if (busy) return { ok: false, error: "tab is busy" };

    const text = String(task.question || task.content || "").trim();
    if (!text) return { ok: false, error: "empty question" };

    setBusy(true, task.id || "");
    try {
      // Rotate to fresh conversation if too many turns (keeps connector healthy)
      const msgCount = countUserMessages();
      if (ROTATE_AFTER_TURNS > 0 && msgCount >= ROTATE_AFTER_TURNS) {
        log("rotating: " + msgCount + " messages, threshold " + ROTATE_AFTER_TURNS);
        await doFreshStart("turn-count " + msgCount + " >= " + ROTATE_AFTER_TURNS);
      }

      // Also rotate if connector dropped during previous answer
      if (connectorState.state === "disconnected") {
        log("rotating: connector disconnected, trying fresh chat");
        await doFreshStart("connector-disconnected");
      }

      const idle = await waitForComposerIdle(900000);
      if (!idle) throw new Error("composer stayed busy");
      if (!(await injectPrompt(text))) throw new Error("prompt injection failed");
      if (!(await clickSend())) throw new Error("send did not take effect");
      turnCount++;
      log("sent task", task.id, "channel", CFG.channel, "turn", turnCount);
      return { ok: true };
    } catch (err) {
      setBusy(false, "");
      return { ok: false, error: err.message || String(err) };
    }
  }

  // ── Connector health monitoring ──

  function setConnectorState(newState, details) {
    const prev = connectorState.state;
    if (prev !== newState) {
      connectorState.state = newState;
      connectorState.lastTransition = new Date().toISOString();
      log("connector state:", prev, "->", newState, details || "");
    }
    if (newState === "stuck" && !connectorState.stuckSince) {
      connectorState.stuckSince = Date.now();
    } else if (newState !== "stuck") {
      connectorState.stuckSince = null;
    }
  }

  function addNetworkEvent(type, detail) {
    connectorState.networkEvents.push({
      type,
      at: new Date().toISOString(),
      ...detail,
    });
    if (connectorState.networkEvents.length > CONNECTOR_EVENT_CAP) {
      connectorState.networkEvents = connectorState.networkEvents.slice(-CONNECTOR_EVENT_CAP);
    }
  }

  function checkConnectorDOM() {
    // Check for "Preparing app tools" spinner — indicates stuck connector init.
    try {
      const main = document.querySelector("main") || document.body;
      const text = (main.innerText || "").slice(-2000);

      if (/Preparing app tools/i.test(text)) {
        setConnectorState("initializing", "Preparing app tools visible");
        if (connectorState.stuckSince && Date.now() - connectorState.stuckSince > 60000) {
          setConnectorState("stuck", "Preparing app tools for >" +
            Math.round((Date.now() - connectorState.stuckSince) / 1000) + "s");
        }
        return;
      }

      // Check for disconnection prompts.
      if (/GitHub (is )?disconnected|reconnect.*GitHub|connector.*error/i.test(text)) {
        setConnectorState("disconnected", "disconnect prompt in DOM");
        return;
      }
    } catch (_) {}

    // Check for connector icon/badge in the composer area.
    try {
      // ChatGPT shows a GitHub icon or "Connected" indicator near the composer
      // when the connector is active. Look for known patterns.
      const connectorBtns = document.querySelectorAll(
        '[aria-label*="onnect" i], [data-testid*="connector"], [data-testid*="github"]'
      );
      if (connectorBtns.length > 0) {
        // Icon present — likely connected or at least visible.
        const anyDisabled = Array.from(connectorBtns).some((btn) =>
          btn.disabled || btn.getAttribute("aria-disabled") === "true" ||
          /disconnect|error|failed/i.test(btn.textContent || "")
        );
        if (anyDisabled) {
          setConnectorState("disconnected", "connector button disabled/error");
        } else {
          setConnectorState("connected", "connector button present");
        }
        return;
      }
    } catch (_) {}

    // No connector element found — icon disappeared.
    // Only flag if we previously saw it (transition from connected → gone).
    if (connectorState.state === "connected") {
      addNetworkEvent("connector_icon_vanished", {
        detail: "connector DOM element disappeared",
        wasProcessing: processing,
      });
      log("connector icon VANISHED (was connected, processing=" + processing + ")");
      // Don't immediately mark disconnected — it may come back after answer.
      // Mark as "flickering" and track.
      setConnectorState("initializing", "connector icon vanished during generation — waiting for recovery");
    } else if (connectorState.state === "initializing") {
      // Already tracking a vanish — check if stuck too long (>30s = likely gone for good)
      if (connectorState.stuckSince && Date.now() - connectorState.stuckSince > 30000) {
        setConnectorState("disconnected", "connector icon gone >30s — likely unlinked");
        addNetworkEvent("connector_icon_lost", {
          goneForMs: Date.now() - connectorState.stuckSince,
          wasProcessing: processing,
        });
        log("connector icon LOST — gone for " +
          Math.round((Date.now() - connectorState.stuckSince) / 1000) + "s");
      }
    }
  }

  // Handle probe events from connector-probe.js (MAIN world).
  function handleProbeEvent(data) {
    const event = data.__agpEvent;
    if (!event) return;

    switch (event) {
      case "connector_used":
        setConnectorState("connected", "connector used in conversation");
        addNetworkEvent("connector_used", { url: data.url });
        break;
      case "connector_init":
        setConnectorState("initializing", "connector init request");
        addNetworkEvent("connector_init", { method: data.method, url: data.url });
        break;
      case "connector_api":
        addNetworkEvent("connector_api", { method: data.method, url: data.url, bodySnippet: data.bodySnippet });
        break;
      case "connector_response":
        addNetworkEvent("connector_response", { method: data.method, url: data.url, status: data.status, responseBody: data.responseBody });
        if (data.responseBody && /unauthorized|invalid.*token|expired|no.*access/i.test(data.responseBody)) {
          setConnectorState("disconnected", "response indicates auth failure (status " + data.status + ")");
        }
        break;
      case "oauth_request":
        addNetworkEvent("oauth_refresh", { method: data.method, url: data.url });
        break;
      case "connector_auth_error":
        setConnectorState("disconnected", "auth error " + data.status);
        addNetworkEvent("auth_error", { status: data.status, url: data.url, responseBody: data.responseBody });
        break;
    }
  }

  // Listen for postMessage from connector-probe.js.
  window.addEventListener("message", (e) => {
    if (e.source !== window || !e.data) return;
    // Nonce handshake: learn the probe's nonce from its hello.
    if (e.data.__agpHello && e.data.nonce) {
      probeNonce = e.data.nonce;
      log("connector-probe linked, nonce:", probeNonce.slice(0, 8) + "...");
      return;
    }
    // Only accept events with the known nonce.
    if (!probeNonce || e.data.__agpNonce !== probeNonce) return;
    if (e.data.__agpEvent) {
      handleProbeEvent(e.data);
    }
  });

  // Request nonce from probe (in case we loaded after the hello).
  window.postMessage({ __agpHelloReq: true }, "*");

  // Report connector health to server periodically.
  async function reportConnectorHealth() {
    if (!activated || !CFG.channel) return;
    checkConnectorDOM();
    try {
      await api("POST", "/api/connector-health", {
        channel: CFG.channel,
        connector_state: connectorState.state,
        last_transition: connectorState.lastTransition,
        details: connectorState.stuckSince
          ? "Preparing app tools spinner for >" +
            Math.round((Date.now() - connectorState.stuckSince) / 1000) + "s"
          : null,
        network_events: connectorState.networkEvents.slice(-10),
      });
    } catch (_) {
      // Server unreachable; fine.
    }
    // Clear reported events.
    connectorState.networkEvents = [];
  }

  // ── Activation / deactivation ──

  function activate(channel) {
    const next = String(channel || "").trim();
    if (!next) return false;
    CFG.channel = next;
    activated = true;
    saveChannel(next);
    createUI();
    updateBadge();
    registerTab();
    return true;
  }

  function deactivate() {
    activated = false;
    CFG.channel = "";
    busy = false;
    currentTask = "";
    saveChannel("");
    chrome.runtime.sendMessage({ type: "tab-unregister" }, () => void chrome.runtime.lastError);
    const wrap = document.getElementById("ask-gpt-git-wrap");
    if (wrap) wrap.remove();
  }

  // ── Badge UI ──

  function createUI() {
    if (document.getElementById("ask-gpt-git-wrap")) return;
    const wrap = document.createElement("div");
    wrap.id = "ask-gpt-git-wrap";
    wrap.innerHTML = '<div id="ask-gpt-git-badge"></div>';
    const style = document.createElement("style");
    style.textContent = `
      #ask-gpt-git-wrap {
        position: fixed;
        right: 18px;
        bottom: 18px;
        z-index: 2147483647;
        font: 12px/1.35 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      }
      #ask-gpt-git-badge {
        min-width: 150px;
        max-width: 320px;
        padding: 7px 9px;
        border-radius: 6px;
        color: #fff;
        background: #404040;
        box-shadow: 0 4px 16px rgba(0, 0, 0, 0.25);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
    `;
    wrap.appendChild(style);
    document.documentElement.appendChild(wrap);
  }

  function connectorIndicator() {
    const s = connectorState.state;
    if (s === "connected") return " | gh:ok";
    if (s === "disconnected") return " | gh:OFF";
    if (s === "stuck") return " | gh:STUCK";
    if (s === "initializing") return " | gh:init";
    return "";
  }

  function updateBadge() {
    const badge = document.getElementById("ask-gpt-git-badge");
    if (!badge) return;
    const conn = connectorIndicator();
    if (!activated || !CFG.channel) {
      badge.textContent = "git-bridge: inactive";
      badge.style.background = "#5f3b16";
    } else if (busy) {
      badge.textContent = `git-bridge: sent [${CFG.channel}]${conn}`;
      badge.style.background = "#4c1d95";
    } else {
      badge.textContent = `git-bridge: ready [${CFG.channel}]${conn}`;
      badge.style.background = connectorState.state === "disconnected" ? "#7f1d1d"
        : connectorState.state === "stuck" ? "#92400e" : "#166534";
    }
  }

  function restoreActivation() {
    const direct = localStorage.getItem(channelKey()) || "";
    if (direct) {
      activate(direct);
      return;
    }
    const stable = stableStorageKey();
    if (stable) {
      chrome.storage.local.get([stable], (data) => {
        if (data && data[stable]) activate(data[stable]);
        else askServiceWorkerForChannel();
      });
    } else {
      askServiceWorkerForChannel();
    }
  }

  function askServiceWorkerForChannel() {
    chrome.runtime.sendMessage({ type: "what-channel" }, (resp) => {
      if (chrome.runtime.lastError) return;
      if (resp && resp.channel) activate(resp.channel);
    });
  }

  // ── Message listener ──

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || typeof msg !== "object") return false;

    if (msg.type === "activate" || msg.type === "set-channel") {
      const ok = activate(msg.channel || "");
      sendResponse({ ok, channel: CFG.channel });
      return false;
    }

    if (msg.type === "deactivate") {
      deactivate();
      sendResponse({ ok: true });
      return false;
    }

    if (msg.type === "query-state") {
      sendResponse({
        ok: true,
        version: VERSION,
        activated,
        channel: CFG.channel,
        busy,
        currentTask,
        connectorState: connectorState.state,
      });
      return false;
    }

    if (msg.type === "process-task") {
      processTask(msg.task || {}).then(sendResponse);
      return true;
    }

    if (msg.type === "task-done") {
      if (!msg.task || msg.task === currentTask) setBusy(false, "");
      sendResponse({ ok: true });
      return false;
    }

    if (msg.type === "get-connector-health") {
      checkConnectorDOM();
      sendResponse({
        ok: true,
        connector: connectorState.state,
        lastTransition: connectorState.lastTransition,
        recentEvents: connectorState.networkEvents.slice(-5),
      });
      return false;
    }

    return false;
  });

  // ── Expose for console debugging ──
  window.__askGptGitSetChannel = (channel) => {
    activate(channel);
    return CFG.channel;
  };

  window.__askGptGitConnector = () => ({
    state: connectorState.state,
    lastTransition: connectorState.lastTransition,
    stuckSince: connectorState.stuckSince,
    recentEvents: connectorState.networkEvents.slice(-10),
  });

  // ── Startup ──
  restoreActivation();
  setInterval(registerTab, 10000);
  setInterval(reportConnectorHealth, CONNECTOR_HEALTH_INTERVAL);
  setInterval(updateBadge, 5000);  // refresh badge with connector state
})();
