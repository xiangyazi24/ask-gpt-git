# ask-gpt-git

Git-drop-only ChatGPT bridge server. A clean rewrite of `chatgpt-bridge-pr3/server.py` (3100+ lines) keeping **only** the git-drop workflow.

## What is git-drop?

Instead of scraping ChatGPT's DOM for answers (fragile, breaks on UI changes), we tell ChatGPT to **commit its answer to a GitHub repo** via its built-in GitHub connector. The server polls for the commit and marks the task as completed when it lands.

```
ask-gpt.py                    server.py                    ChatGPT
    |                              |                           |
    |-- POST /api/ask ----------->|                           |
    |   {question, channel,       |                           |
    |    gitdrop: {repo,branch,   |                           |
    |             file,baseline}} |                           |
    |<---- {id, status:pending} --|                           |
    |                              |                           |
    |                              |-- GET /api/pending ------>|
    |                              |   (extension polls)       |
    |                              |<-- task ------------------|
    |                              |                           |
    |                              |   [extension sends        |
    |                              |    question to ChatGPT]   |
    |                              |                           |
    |                              |        [ChatGPT commits   |
    |                              |         answer to GitHub] |
    |                              |                           |
    |                              |-- poll GitHub commits --->|
    |                              |   (gitdrop_advance_loop)  |
    |                              |<-- new SHA != baseline ---|
    |                              |                           |
    |                              |   [mark task completed]   |
    |                              |                           |
    |-- GET /api/wait/<id> ------>|                           |
    |<---- {status:completed} ----|                           |
```

## Architecture

```
ask-gpt-git/
├── server.py          # HTTP server + endpoints
├── gitdrop.py         # GitHub commit polling (daemon thread)
├── browser.py         # Browser automation stub (extension handles it)
├── config.json.example
├── requirements.txt   # No external deps (stdlib only)
└── README.md
```

- **server.py** — `http.server`-based threaded HTTP server. Handles task lifecycle (create, dispatch, wait, complete). ~280 lines.
- **gitdrop.py** — Polls GitHub via `gh api` for new commits on the drop file. When a commit lands with a SHA different from the baseline, marks the task completed. ~100 lines.
- **browser.py** — Stub. The Chrome extension handles sending questions to ChatGPT tabs by polling `/api/pending`. This file is a future extension point for headless automation.

## API Endpoints

### GET

| Endpoint | Description |
|---|---|
| `/api/status` | Server status: task counts by status, active channels, uptime |
| `/api/healthz` | Liveness probe |
| `/api/channels` | List active channels |
| `/api/pending?channel=NAME` | Extension polls this to pick up tasks (also serves as channel heartbeat) |
| `/api/result/<id>` | Get a task's result |
| `/api/wait/<id>?timeout=N` | Long-poll until task completes (default 600s, max 2700s) |

### POST

| Endpoint | Body | Description |
|---|---|---|
| `/api/ask` | `{question, channel, gitdrop: {repo, branch, file, baseline}}` | Submit a question |
| `/api/gitdrop-done` | `{task, channel, sha}` | External notification that a commit landed |
| `/api/respond` | `{task}` | Extension confirms it sent the question |
| `/api/clear` | `{}` | Clear all tasks |

## Running

```bash
# No dependencies to install (stdlib only).
# Requires: gh (GitHub CLI) installed and authenticated.

python3 server.py          # default port 8801
python3 server.py 9000     # custom port
```

## What was removed (vs chatgpt-bridge-pr3)

- DOM scraping / answer capture from the chat UI
- Tab color detection (purple/green state machine)
- Unattended monitor / health check alerts
- Shadow reports / debug stats
- Account freeze/fence management
- SQLite persistence (bridge_store)
- bridge_config / bridge_classifier / bridge_dom_validation
- Push notification scheduler (tmux inject)
- Needs-manual parking / operator panel
- Image generation endpoints
- Worker health reports / stall detection
- Foreign channel isolation
- Zombie tab detection
- Reset/fresh-start machinery
- Log rotation
- ~2800 lines of complexity
