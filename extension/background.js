// ChatGPT Git-Drop Bridge: background worker.
//
// This is intentionally git-only. The extension sends prompts to ChatGPT and
// waits for the local server to report that a git-drop commit landed. It never
// captures or submits ChatGPT's DOM answer as the result.

const EXT_VERSION = "1.0.0";
const POLL_INTERVAL_MS = 1500;
const STALE_TAB_MS = 120000;
const BRIDGE_CANDIDATES = [
  "http://localhost:8801",
  "http://127.0.0.1:8801",
  "http://100.69.3.30:8801",
  "http://192.168.1.113:8801"
];

const tabs = new Map(); // tabId -> { channel, busy, inFlight, lastSeen }
let pollTimer = null;
let pollRunning = false;

function now() {
  return Date.now();
}

function getBridgeUrl() {
  return new Promise((resolve) => {
    chrome.storage.local.get(["bridgeUrl"], (data) => {
      resolve((data && data.bridgeUrl) || "http://localhost:8801");
    });
  });
}

async function setBridgeUrl(url) {
  await new Promise((resolve) => chrome.storage.local.set({ bridgeUrl: url }, resolve));
}

async function apiFetch(base, method, path, body) {
  const ctrl = new AbortController();
  const timeout = setTimeout(() => ctrl.abort(), 10000);
  const opts = { method: method || "GET", signal: ctrl.signal };
  if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  try {
    const resp = await fetch(base + path, opts);
    const text = await resp.text();
    let data = {};
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (_) {
        data = { raw: text };
      }
    }
    if (!resp.ok) {
      const err = new Error(data.error || `HTTP ${resp.status}`);
      err.status = resp.status;
      err.data = data;
      throw err;
    }
    return data;
  } finally {
    clearTimeout(timeout);
  }
}

async function probeBridge(url) {
  try {
    const data = await apiFetch(url, "GET", "/api/status");
    return !!(data && data.ok);
  } catch (_) {
    return false;
  }
}

async function pickBridgeUrl(current) {
  const seen = new Set();
  const order = [current, ...BRIDGE_CANDIDATES].filter((url) => {
    if (!url || seen.has(url)) return false;
    seen.add(url);
    return true;
  });
  for (const url of order) {
    if (await probeBridge(url)) {
      if (url !== current) await setBridgeUrl(url);
      return url;
    }
  }
  return current;
}

function sendToTab(tabId, message) {
  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(tabId, message, (resp) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
      } else {
        resolve(resp || {});
      }
    });
  });
}

function touchTab(tabId, patch) {
  const prev = tabs.get(tabId) || {};
  const next = { ...prev, ...patch, lastSeen: now() };
  if (next.channel) tabs.set(tabId, next);
  return next;
}

function removeTab(tabId) {
  tabs.delete(tabId);
}

async function queryChatGptTabs() {
  return await new Promise((resolve) => {
    chrome.tabs.query(
      { url: ["https://chatgpt.com/*", "https://chat.openai.com/*"] },
      (matched) => resolve(matched || [])
    );
  });
}

async function recoverTabsFromContentScripts() {
  const matched = await queryChatGptTabs();
  const liveIds = new Set(matched.map((tab) => tab.id));
  for (const tabId of Array.from(tabs.keys())) {
    if (!liveIds.has(tabId)) removeTab(tabId);
  }
  for (const tab of matched) {
    try {
      const state = await sendToTab(tab.id, { type: "query-state" });
      if (state && state.activated && state.channel) {
        const prev = tabs.get(tab.id) || {};
        touchTab(tab.id, {
          channel: state.channel,
          busy: !!state.busy || !!prev.inFlight,
          inFlight: prev.inFlight || state.currentTask || ""
        });
      }
    } catch (_) {
      // Content script not ready in this tab.
    }
  }
}

async function markTaskDone(tabId, task, status) {
  const info = tabs.get(tabId);
  if (!info || info.inFlight !== task) return;
  touchTab(tabId, { busy: false, inFlight: "" });
  try {
    await sendToTab(tabId, { type: "task-done", task, status });
  } catch (_) {}
}

async function refreshInflight(base) {
  for (const [tabId, info] of Array.from(tabs.entries())) {
    if (!info.inFlight) continue;
    try {
      const result = await apiFetch(base, "GET", `/api/result/${encodeURIComponent(info.inFlight)}`);
      if (result && (result.status === "completed" || result.status === "failed")) {
        await markTaskDone(tabId, info.inFlight, result.status);
      }
    } catch (err) {
      if (err.status === 404) {
        await markTaskDone(tabId, info.inFlight, "gone");
      }
    }
  }
}

async function dispatchOne(base, tabId, info) {
  if (!info.channel || info.inFlight || info.busy) return;

  let state;
  try {
    state = await sendToTab(tabId, { type: "query-state" });
  } catch (_) {
    removeTab(tabId);
    return;
  }
  if (!state || !state.activated || state.busy || state.channel !== info.channel) {
    touchTab(tabId, { busy: !!(state && state.busy), channel: (state && state.channel) || info.channel });
    return;
  }

  let task;
  try {
    task = await apiFetch(
      base,
      "GET",
      `/api/pending?channel=${encodeURIComponent(info.channel)}&ext=${encodeURIComponent(EXT_VERSION)}`
    );
  } catch (_) {
    return;
  }
  if (!task || !task.id) return;

  touchTab(tabId, { busy: true, inFlight: task.id });
  try {
    const sent = await sendToTab(tabId, { type: "process-task", task });
    if (sent && sent.ok) {
      await apiFetch(base, "POST", "/api/respond", {
        task: task.id,
        sent: true,
        extension: `ask-gpt-git/${EXT_VERSION}`
      });
    } else {
      console.warn("[ask-gpt-git] prompt dispatch failed", task.id, sent && sent.error);
      touchTab(tabId, { busy: false, inFlight: "" });
      await nackTask(base, task.id, (sent && sent.error) || "dispatch_failed");
    }
  } catch (err) {
    console.warn("[ask-gpt-git] prompt dispatch error", task.id, err.message);
    touchTab(tabId, { busy: false, inFlight: "" });
    await nackTask(base, task.id, err.message || "dispatch_error");
  }
}

async function nackTask(base, taskId, reason) {
  try {
    await apiFetch(base, "POST", `/api/nack/${encodeURIComponent(taskId)}`, {
      reason,
      extension: `ask-gpt-git/${EXT_VERSION}`
    });
  } catch (_) {}
}

async function pollOnce() {
  if (pollRunning) return;
  pollRunning = true;
  try {
    await recoverTabsFromContentScripts();
    const cutoff = now() - STALE_TAB_MS;
    for (const [tabId, info] of Array.from(tabs.entries())) {
      if ((info.lastSeen || 0) < cutoff && !info.inFlight) removeTab(tabId);
    }
    if (tabs.size === 0) return;

    let base = await getBridgeUrl();
    base = await pickBridgeUrl(base);

    await refreshInflight(base);
    for (const [tabId, info] of Array.from(tabs.entries())) {
      await dispatchOne(base, tabId, info);
    }
  } finally {
    pollRunning = false;
  }
}

function ensurePolling() {
  if (pollTimer) return;
  pollTimer = setInterval(() => {
    pollOnce().catch((err) => console.warn("[ask-gpt-git] poll error", err.message));
  }, POLL_INTERVAL_MS);
  pollOnce().catch(() => {});
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.get(["bridgeUrl"], (data) => {
    if (!data.bridgeUrl) chrome.storage.local.set({ bridgeUrl: "http://localhost:8801" });
  });
  chrome.tabs.query({ url: ["https://chatgpt.com/*", "https://chat.openai.com/*"] }, (matched) => {
    for (const tab of matched || []) {
      chrome.tabs.reload(tab.id, {}, () => void chrome.runtime.lastError);
    }
  });
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const tabId = sender && sender.tab && sender.tab.id;

  if (msg && msg.type === "tab-register" && tabId != null) {
    touchTab(tabId, { channel: msg.channel || "", busy: !!msg.busy, inFlight: msg.currentTask || "" });
    ensurePolling();
    sendResponse({ ok: true });
    return false;
  }

  if (msg && msg.type === "tab-state" && tabId != null) {
    touchTab(tabId, { channel: msg.channel || "", busy: !!msg.busy, inFlight: msg.currentTask || "" });
    ensurePolling();
    sendResponse({ ok: true });
    return false;
  }

  if (msg && msg.type === "tab-unregister" && tabId != null) {
    removeTab(tabId);
    sendResponse({ ok: true });
    return false;
  }

  if (msg && msg.type === "what-channel" && tabId != null) {
    const info = tabs.get(tabId) || {};
    sendResponse({ ok: true, channel: info.channel || "" });
    return false;
  }

  if (msg && msg.type === "bridge") {
    getBridgeUrl()
      .then((base) => apiFetch(base, msg.method || "GET", msg.path, msg.body))
      .then((data) => sendResponse({ ok: true, data }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
  }

  return false;
});

chrome.tabs.onRemoved.addListener((tabId) => removeTab(tabId));

chrome.alarms.create("ask-gpt-git-poll", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "ask-gpt-git-poll") pollOnce().catch(() => {});
});

recoverTabsFromContentScripts().then(ensurePolling).catch(() => ensurePolling());
