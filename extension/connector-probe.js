// connector-probe.js — MAIN world fetch interceptor for GitHub connector monitoring.
//
// Runs in the page's JS context (MAIN world, document_start) so it can wrap
// fetch() before ChatGPT's own code runs. Watches for connector-related API
// traffic and forwards events to the content script via postMessage with a
// per-page nonce for forgery resistance.
//
// This script is READ-ONLY: it never modifies requests or responses.

(() => {
  "use strict";

  const GEN = 1;
  if (window.__askGptGitProbeInstalled &&
      window.__askGptGitProbeInstalled.generation >= GEN) return;
  window.__askGptGitProbeInstalled = { generation: GEN, version: "1.0.0" };

  // Per-page nonce. Not a cryptographic secret — just a speed-bump against
  // co-resident page scripts forging probe events.
  const NONCE = "agp-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
  window.postMessage({ __agpHello: true, nonce: NONCE, generation: GEN }, "*");

  // Content script can request the nonce if it loaded after our hello.
  window.addEventListener("message", (e) => {
    if (e.source !== window || !e.data || !e.data.__agpHelloReq) return;
    window.postMessage({ __agpHello: true, nonce: NONCE, generation: GEN }, "*");
  });

  function emit(obj) {
    try { window.postMessage(Object.assign({ __agpNonce: NONCE }, obj), "*"); }
    catch (_) {}
  }

  // Patterns that indicate connector-related traffic.
  const CONNECTOR_RE = /connector|github|gitmcp|repo|source|integration|oauth|token/i;
  const BACKEND_API_RE = /backend-api/;
  const CONVERSATION_RE = /backend-api\/(f\/)?conversation/;
  const FILES_CONNECTOR_RE = /backend-api\/files\/(connector|gitmcp)/;
  const OAUTH_RE = /github\.com\/login\/oauth|api\.github\.com\/.*token/;

  const origFetch = window.fetch;

  window.fetch = function (input, init) {
    let url = "", method = "GET", body = null;
    try {
      url = typeof input === "string" ? input : (input && input.url) || "";
      method = String((init && init.method) ||
                      (typeof input === "object" && input && input.method) ||
                      "GET").toUpperCase();
      body = init && init.body;
    } catch (_) {}

    const urlStr = String(url);
    const bodyStr = typeof body === "string" ? body : "";

    // 1. Conversation POST with connector content — connector is being used.
    try {
      if (method === "POST" && CONVERSATION_RE.test(urlStr) && bodyStr.length < 500000) {
        if (/connector|gitmcp|github/i.test(bodyStr)) {
          emit({
            __agpEvent: "connector_used",
            url: urlStr.slice(0, 200),
            ts: Date.now(),
          });
        }
      }
    } catch (_) {}

    // 2. Connector init/status requests (files/connector/...).
    try {
      if (BACKEND_API_RE.test(urlStr) && FILES_CONNECTOR_RE.test(urlStr)) {
        emit({
          __agpEvent: "connector_init",
          method,
          url: urlStr.slice(0, 300),
          ts: Date.now(),
        });
      }
    } catch (_) {}

    // 3. Any backend-api call with connector/github keywords.
    try {
      if (method !== "GET" && BACKEND_API_RE.test(urlStr)) {
        const sig = urlStr + " " + bodyStr.slice(0, 3000);
        if (CONNECTOR_RE.test(sig)) {
          emit({
            __agpEvent: "connector_api",
            method,
            url: urlStr.slice(0, 300),
            bodySnippet: bodyStr.slice(0, 500),
            ts: Date.now(),
          });
        }
      }
    } catch (_) {}

    // 4. OAuth endpoint requests.
    try {
      if (OAUTH_RE.test(urlStr)) {
        emit({
          __agpEvent: "oauth_request",
          method,
          url: urlStr.slice(0, 300),
          ts: Date.now(),
        });
      }
    } catch (_) {}

    // Call original fetch and watch for error responses on connector traffic.
    const result = origFetch.apply(this, arguments);

    try {
      if (BACKEND_API_RE.test(urlStr) && CONNECTOR_RE.test(urlStr + " " + bodyStr.slice(0, 1000))) {
        result.then((resp) => {
          try {
            if (resp.status === 401 || resp.status === 403) {
              emit({
                __agpEvent: "connector_auth_error",
                method,
                url: urlStr.slice(0, 300),
                status: resp.status,
                ts: Date.now(),
              });
            }
          } catch (_) {}
        }).catch(() => {});
      }
    } catch (_) {}

    return result;
  };

  console.log("[ask-gpt-git] connector-probe installed (gen " + GEN + ")");
})();
