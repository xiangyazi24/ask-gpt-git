document.addEventListener("DOMContentLoaded", () => {
  const bridgeUrl = document.getElementById("bridge-url");
  const channel = document.getElementById("channel");
  const channels = document.getElementById("channels");
  const out = document.getElementById("out");
  const connStatus = document.getElementById("connector-status");

  function show(message, kind) {
    out.textContent = message;
    out.className = kind || "ok";
    out.style.display = "block";
  }

  function activeTab() {
    return new Promise((resolve) => {
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => resolve(tabs && tabs[0]));
    });
  }

  async function sendToActiveTab(message) {
    const tab = await activeTab();
    if (!tab || !tab.id) throw new Error("No active tab");
    return await new Promise((resolve, reject) => {
      chrome.tabs.sendMessage(tab.id, message, (resp) => {
        if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
        else resolve(resp || {});
      });
    });
  }

  async function bridge(method, path, body) {
    return await new Promise((resolve, reject) => {
      chrome.runtime.sendMessage({ type: "bridge", method, path, body }, (resp) => {
        if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
        else if (!resp || !resp.ok) reject(new Error((resp && resp.error) || "bridge request failed"));
        else resolve(resp.data);
      });
    });
  }

  function refreshChannels() {
    bridge("GET", "/api/channels")
      .then((data) => {
        channels.innerHTML = "";
        for (const ch of data.channels || []) {
          const opt = document.createElement("option");
          opt.value = ch;
          channels.appendChild(opt);
        }
      })
      .catch(() => {});
  }

  function updateConnectorDisplay() {
    sendToActiveTab({ type: "get-connector-health" })
      .then((resp) => {
        if (!resp || !resp.ok) {
          connStatus.textContent = "tab not active";
          connStatus.className = "connector-status conn-unknown";
          return;
        }
        const s = resp.connector || "unknown";
        const cls = s === "connected" ? "conn-ok"
          : s === "disconnected" ? "conn-off"
          : s === "initializing" || s === "stuck" ? "conn-init"
          : "conn-unknown";
        let label = s;
        if (resp.lastTransition) {
          const d = new Date(resp.lastTransition);
          label += " (since " + d.toLocaleTimeString() + ")";
        }
        if (resp.recentEvents && resp.recentEvents.length > 0) {
          label += "\nrecent: " + resp.recentEvents.map((e) => e.type).join(", ");
        }
        connStatus.textContent = label;
        connStatus.className = "connector-status " + cls;
      })
      .catch(() => {
        connStatus.textContent = "no content script";
        connStatus.className = "connector-status conn-unknown";
      });
  }

  // Initialize.
  chrome.storage.local.get(["bridgeUrl"], (data) => {
    bridgeUrl.value = (data && data.bridgeUrl) || "http://localhost:8801";
    refreshChannels();
  });

  sendToActiveTab({ type: "query-state" })
    .then((state) => {
      if (state && state.channel) channel.value = state.channel;
      if (state && state.activated) show(`Active: ${state.channel}${state.busy ? " (busy)" : ""}`, "ok");
    })
    .catch(() => {});

  updateConnectorDisplay();

  // Handlers.
  document.getElementById("save").onclick = () => {
    chrome.storage.local.set({ bridgeUrl: bridgeUrl.value.trim() || "http://localhost:8801" }, () => {
      show("Saved.", "ok");
      refreshChannels();
    });
  };

  document.getElementById("test").onclick = async () => {
    try {
      await new Promise((resolve) => chrome.storage.local.set({
        bridgeUrl: bridgeUrl.value.trim() || "http://localhost:8801"
      }, resolve));
      const status = await bridge("GET", "/api/status");
      show(`OK. Channels: ${(status.channels || []).join(", ") || "(none)"}`, "ok");
      refreshChannels();
    } catch (err) {
      show(err.message, "err");
    }
  };

  document.getElementById("activate").onclick = async () => {
    try {
      const ch = channel.value.trim();
      if (!ch) throw new Error("Channel is required");
      await new Promise((resolve) => chrome.storage.local.set({
        bridgeUrl: bridgeUrl.value.trim() || "http://localhost:8801"
      }, resolve));
      const resp = await sendToActiveTab({ type: "activate", channel: ch });
      if (!resp || !resp.ok) throw new Error((resp && resp.error) || "activation failed");
      show(`Activated: ${resp.channel || ch}`, "ok");
      refreshChannels();
      updateConnectorDisplay();
    } catch (err) {
      show(err.message, "err");
    }
  };

  document.getElementById("deactivate").onclick = async () => {
    try {
      await sendToActiveTab({ type: "deactivate" });
      show("Deactivated.", "ok");
    } catch (err) {
      show(err.message, "err");
    }
  };
});
