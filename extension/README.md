# ask-gpt-git Chrome Extension

Minimal Chrome extension for the ask-gpt-git bridge. Git-drop only — no DOM
capture of answers. The extension's job is to:

1. Poll the server for pending tasks (`GET /api/pending`)
2. Type the question into ChatGPT's input and click send
3. Report back to the server when the prompt is dispatched

The answer arrives via git-drop (ChatGPT commits to a GitHub repo via the
GitHub connector). The extension never reads the response from the page.

## Architecture

```
extension/
├── manifest.json          # MV3, minimal permissions
├── background.js          # Service worker: proxy API calls, tab management
├── content.js             # Content script: DOM interaction, task dispatch,
│                          #   connector health monitoring
├── connector-probe.js     # MAIN world: fetch interception for connector
│                          #   health diagnostics
├── popup.html + popup.js  # Status UI with connector health display
└── README.md              # This file
```

## Connector Health Monitoring

The GitHub connector frequently disconnects for unknown reasons. The extension
monitors its state through two complementary channels:

### DOM monitoring (content.js)

Every 60 seconds, content.js scans for:
- "Preparing app tools" spinner (stuck connector init)
- "GitHub disconnected" / reconnect prompts
- Connector icon/badge presence and state

### Network monitoring (connector-probe.js, MAIN world)

Wraps `fetch()` in the page context to intercept:
- `POST /backend-api/conversation` with connector content — connector is active
- `POST /backend-api/files/connector/...` — connector init/status requests
- GitHub OAuth endpoint requests — token refresh in progress
- 401/403 responses on connector-related requests — token expired

Events are forwarded to content.js via `postMessage` with a per-page nonce.

### Health reporting

Content.js reports connector state to the server every 60s via:
```
POST /api/connector-health
{
  "channel": "dm1",
  "connector_state": "connected|disconnected|initializing|stuck|unknown",
  "last_transition": "2026-06-30T...",
  "details": "...",
  "network_events": [...]
}
```

The server logs state transitions to `disconnected` or `stuck` for post-mortem
analysis. Connector health is also included in `GET /api/status`.

## Installation

1. Open `chrome://extensions/`
2. Enable Developer mode
3. Click "Load unpacked" and select this `extension/` directory
4. Open a ChatGPT tab
5. Click the extension icon, set the bridge URL and channel name, click Activate

## Server

The extension talks to `server.py` on `localhost:8801` (configurable in the
popup). The server auto-discovers bridge URLs across localhost, LAN, and
Tailscale.
