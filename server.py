#!/usr/bin/env python3
"""ask-gpt-git server — git-drop-only ChatGPT bridge.

A stripped-down rewrite of chatgpt-bridge-pr3/server.py (3100+ lines) keeping
ONLY the git-drop workflow: route questions to ChatGPT browser tabs via the
Chrome extension, wait for the answer to land as a git commit, report
completion.

No DOM scraping. No tab color detection. No unattended monitor. No shadow
reports. No freeze/fence machinery. Just the git-drop path.

Usage:
    python3 server.py [PORT]          # default 8801
"""

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from gitdrop import gitdrop_advance_loop
import browser

# ── Configuration ────────────────────────────────────────────────────────────

SERVER_VERSION = "1.0.0"
TASK_TTL = 3600                 # completed/failed tasks kept in memory for 1h
PENDING_TTL = 7 * 86400        # pending tasks expire after 7 days
PROCESSING_TIMEOUT = 2700      # processing tasks time out after 45 min
CHANNEL_TIMEOUT = 3600         # channel heartbeat timeout (1h)
QUEUE_MAX = 200                # max pending tasks before refusing new ones

# ── State ────────────────────────────────────────────────────────────────────

tasks = {}                      # task_id -> task dict
channels = {}                   # channel_name -> last heartbeat timestamp
channel_groups = {}             # group_name -> {"channels": set(), "updated": ts}
channel_seq = {}                # channel_name -> next sequence number
task_events = {}                # task_id -> threading.Event (signaled on completion)
connector_health = {}           # channel_name -> latest connector health report
lock = threading.Lock()
_start_time = time.time()


# ── Task helpers ─────────────────────────────────────────────────────────────

def apply_result(tid, answer, provenance="gitdrop"):
    """Mark a task as completed. Caller holds lock."""
    t = tasks.get(tid)
    if t is None:
        return "unknown_task"
    if t.get("status") == "completed":
        return "already_completed"
    now = time.time()
    t["answer"] = answer
    t["status"] = "completed"
    t["completed_at"] = now
    t["updated"] = now
    t["provenance"] = provenance
    evt = task_events.get(tid)
    if evt:
        evt.set()
    return "applied"


def _active_channels():
    """Return sorted list of channels seen within CHANNEL_TIMEOUT. Caller holds lock."""
    now = time.time()
    return sorted(ch for ch, ts in channels.items()
                  if now - ts < CHANNEL_TIMEOUT)


def _channel_stats():
    """Return per-channel task counts for status/auto-dispatch. Caller holds lock."""
    now = time.time()
    out = {}
    for ch, ts in channels.items():
        out.setdefault(ch, {
            "pending": 0,
            "processing": 0,
            "completed": 0,
            "failed": 0,
            "last_seen_s": int(now - ts),
        })
    for t in tasks.values():
        ch = t.get("channel", "")
        info = out.setdefault(ch, {
            "pending": 0,
            "processing": 0,
            "completed": 0,
            "failed": 0,
            "last_seen_s": None,
        })
        st = t.get("status", "")
        if st in info:
            info[st] += 1
    return out


def _cleanup():
    """Expire stale tasks and channels. Caller holds lock."""
    now = time.time()
    active = set(_active_channels())

    # Expire old pending tasks (channel offline + TTL)
    for t in tasks.values():
        if (t["status"] == "pending"
                and now - t["created"] > PENDING_TTL
                and t.get("channel", "") not in active):
            t["status"] = "failed"
            t["answer"] = "[FAILED] no consumer, expired after %ds" % int(now - t["created"])
            t["updated"] = now
            evt = task_events.get(t["id"])
            if evt:
                evt.set()

    # Time out processing tasks
    for t in tasks.values():
        if t["status"] == "processing" and now - t["updated"] > PROCESSING_TIMEOUT:
            t["status"] = "failed"
            t["answer"] = "[FAILED] processing timeout after %ds" % int(now - t["updated"])
            t["updated"] = now
            evt = task_events.get(t["id"])
            if evt:
                evt.set()

    # Delete old completed/failed tasks
    stale = [k for k, v in tasks.items()
             if v["status"] in ("completed", "failed") and now - v["updated"] > TASK_TTL]
    for k in stale:
        del tasks[k]
        task_events.pop(k, None)

    # Clean dead channels
    dead = [ch for ch, ts in channels.items() if now - ts > TASK_TTL]
    for ch in dead:
        del channels[ch]

    # Prune empty groups
    for g in list(channel_groups):
        info = channel_groups[g]
        info["channels"] = {m for m in info["channels"] if m not in dead}
        if not info["channels"]:
            del channel_groups[g]


# ── HTTP Handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    ALLOWED_ORIGINS = {"https://chatgpt.com", "https://chat.openai.com"}

    def _cors(self):
        origin = self.headers.get("Origin", "")
        if origin in self.ALLOWED_ORIGINS or origin.startswith("chrome-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        else:
            self.send_header("Access-Control-Allow-Origin", "https://chatgpt.com")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return {}

    def _query(self, key):
        qs = parse_qs(urlparse(self.path).query)
        vals = qs.get(key)
        return vals[0] if vals else None

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # ── GET endpoints ────────────────────────────────────────────────────

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/api/status":
            with lock:
                _cleanup()
                by_status = {}
                for t in tasks.values():
                    by_status[t["status"]] = by_status.get(t["status"], 0) + 1
                now = time.time()
                uptime = int(now - _start_time)
                # Build connector health summary for active channels.
                conn_summary = {}
                for ch in _active_channels():
                    report = connector_health.get(ch)
                    if report:
                        conn_summary[ch] = {
                            "state": report.get("connector_state", "unknown"),
                            "last_transition": report.get("last_transition"),
                            "details": report.get("details"),
                            "age_s": int(now - report.get("reported_at", now)),
                        }
                self._json({
                    "ok": True,
                    **by_status,
                    "total": len(tasks),
                    "channels": _active_channels(),
                    "by_channel": _channel_stats(),
                    "groups": {g: sorted(info["channels"])
                               for g, info in channel_groups.items()},
                    "connector_health": conn_summary,
                    "uptime_s": uptime,
                    "uptime": "%dh%dm" % (uptime // 3600, (uptime % 3600) // 60),
                })

        elif path == "/api/healthz":
            self._json({"ok": True, "uptime_s": int(time.time() - _start_time)})

        elif path == "/api/channels":
            with lock:
                self._json({"channels": _active_channels()})

        elif path == "/api/pending":
            # Extension polls this to pick up the next task for a channel.
            ch = self._query("channel")
            with lock:
                _cleanup()
                if ch is not None:
                    channels[ch] = time.time()
                    # Auto-group: channel ending with digits -> group by prefix
                    if ch and ch[-1].isdigit():
                        group_name = ch.rstrip("0123456789").rstrip("-_")
                        if group_name and group_name != ch and group_name not in channels:
                            if group_name not in channel_groups:
                                channel_groups[group_name] = {
                                    "channels": set(), "updated": time.time()}
                            if ch not in channel_groups[group_name]["channels"]:
                                channel_groups[group_name]["channels"].add(ch)
                                channel_groups[group_name]["updated"] = time.time()

                pending = [t for t in tasks.values()
                           if t["status"] == "pending"
                           and (ch is None or t.get("channel", "") == ch)]
                if pending:
                    task = min(pending, key=lambda t: t["created"])
                    task["status"] = "processing"
                    task["updated"] = time.time()
                    task["attempt_id"] = uuid.uuid4().hex[:12]
                    self._json(task)
                else:
                    self._json({})

        elif path.startswith("/api/result/"):
            tid = path.split("/")[-1]
            with lock:
                t = tasks.get(tid)
                if t and t.get("status") == "completed" and not t.get("retrieved_at"):
                    t["retrieved_at"] = time.time()
                self._json(t if t else {"error": "not found"}, 200 if t else 404)

        elif path.startswith("/api/wait/"):
            tid = path.split("/")[-1]
            raw_timeout = self._query("timeout")
            try:
                timeout = min(float(raw_timeout), 2700) if raw_timeout else 600
            except ValueError:
                timeout = 600
            with lock:
                t = tasks.get(tid)
            if not t:
                self._json({"error": "not found"}, 404)
                return
            if t.get("status") in ("completed", "failed"):
                with lock:
                    if not t.get("retrieved_at"):
                        t["retrieved_at"] = time.time()
                self._json(t)
                return
            evt = task_events.get(tid)
            if not evt:
                self._json({"error": "no event for task"}, 500)
                return
            evt.wait(timeout=timeout)
            with lock:
                t = tasks.get(tid)
                if t and t.get("status") == "completed" and not t.get("retrieved_at"):
                    t["retrieved_at"] = time.time()
            if t and t.get("status") in ("completed", "failed"):
                self._json(t)
            else:
                self._json({"error": "timeout", "id": tid,
                            "status": t.get("status") if t else "gone"}, 408)

        elif path == "/api/connector-health":
            ch = self._query("channel")
            with lock:
                if ch:
                    report = connector_health.get(ch, {})
                    self._json({"ok": True, "channel": ch, **report})
                else:
                    self._json({"ok": True, "channels": {
                        k: v for k, v in connector_health.items()
                    }})

        else:
            self._json({"error": "not found"}, 404)

    # ── POST endpoints ───────────────────────────────────────────────────

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()

        if path == "/api/ask":
            tid = uuid.uuid4().hex[:8]
            now = time.time()
            ch = body.get("channel", "")
            q = body.get("question", "") or body.get("content", "")
            if not isinstance(q, str) or not q.strip():
                self._json({"error": "question is required"}, 400)
                return
            q = q.strip()

            with lock:
                _cleanup()
                active = _active_channels()

                # Group routing: if ch matches a group name, pick the
                # least-loaded active member.
                resolved_group = None
                if ch and ch in channel_groups:
                    members = [m for m in channel_groups[ch]["channels"]
                               if m in active]
                    if members:
                        def _load(m):
                            return sum(1 for t in tasks.values()
                                       if t.get("channel") == m
                                       and t["status"] in ("pending", "processing"))
                        members.sort(key=_load)
                        resolved_group = ch
                        ch = members[0]

                n_pending = sum(1 for t in tasks.values()
                                if t["status"] == "pending")
                if n_pending >= QUEUE_MAX:
                    self._json({"error": "queue_full", "pending": n_pending}, 503)
                    return

                seq = channel_seq.get(ch, 1)
                channel_seq[ch] = seq + 1
                routable = (not ch) or (ch in active)
                task_events[tid] = threading.Event()
                tasks[tid] = {
                    "id": tid,
                    "seq": seq,
                    "channel": ch,
                    "question": q,
                    "status": "pending",
                    "answer": None,
                    "created": now,
                    "updated": now,
                    # git-drop target + baseline SHA (ask-gpt.py supplies this)
                    "gitdrop": body.get("gitdrop") or None,
                }

            resp = {
                "id": tid,
                "seq": seq,
                "status": "pending",
                "channel": ch,
                "routable": routable,
                "active_channels": active,
            }
            if resolved_group:
                resp["group"] = resolved_group
                resp["resolved_to"] = ch
            self._json(resp)

        elif path == "/api/gitdrop-done":
            # External notification that a git-drop commit landed.
            # ask-gpt.py calls this when its own commit-poll detects the new SHA.
            tid = body.get("task", "")
            ch = body.get("channel", "")
            sha = body.get("sha", "")
            with lock:
                t = tasks.get(tid)
                if t is not None and t.get("status") not in ("completed", "failed"):
                    apply_result(tid,
                                 "[git-drop] committed %s — answer is in the repo." % sha,
                                 provenance="gitdrop")
            _log("gitdrop-done %s %s %s", ch, tid[:8] if tid else "?",
                 sha[:9] if sha else "?")
            self._json({"ok": True, "channel": ch, "task": tid})

        elif path == "/api/respond":
            # Extension reports that it sent the question to ChatGPT.
            # In git-drop mode we just acknowledge; the completion signal
            # comes from the commit, not from DOM scraping.
            tid = body.get("task", body.get("id", ""))
            with lock:
                t = tasks.get(tid)
                if t and t.get("status") == "pending":
                    t["status"] = "processing"
                    t["updated"] = time.time()
            self._json({"ok": True})

        elif path == "/api/nack" or path.startswith("/api/nack/"):
            # Extension could not inject/send the prompt. Requeue a few times;
            # do not treat any browser-side content as the answer.
            tid = body.get("task", body.get("id", ""))
            if not tid and path.startswith("/api/nack/"):
                tid = path.split("/")[-1]
            reason = body.get("reason", "dispatch_failed")
            with lock:
                t = tasks.get(tid)
                if not t:
                    self._json({"ok": False, "error": "not found"}, 404)
                    return
                retries = int(t.get("retries", 0)) + 1
                t["retries"] = retries
                t["last_nack"] = reason
                t["updated"] = time.time()
                if t.get("status") not in ("completed", "failed"):
                    if retries >= 5:
                        t["status"] = "failed"
                        t["answer"] = "[FAILED] dispatch failed repeatedly: %s" % reason
                        evt = task_events.get(tid)
                        if evt:
                            evt.set()
                    else:
                        t["status"] = "pending"
                status = t.get("status")
            self._json({"ok": True, "task": tid, "status": status, "retries": retries})

        elif path == "/api/connector-health":
            ch = body.get("channel", "")
            state = body.get("connector_state", "unknown")
            now = time.time()
            report = {
                "channel": ch,
                "connector_state": state,
                "last_transition": body.get("last_transition"),
                "details": body.get("details"),
                "network_events": body.get("network_events", []),
                "reported_at": now,
            }
            with lock:
                prev = connector_health.get(ch, {})
                prev_state = prev.get("connector_state", "unknown")
                connector_health[ch] = report
            if state != prev_state and state in ("disconnected", "stuck"):
                _log("CONNECTOR %s: %s → %s  %s",
                     ch, prev_state, state,
                     body.get("details") or "")
            self._json({"ok": True, "channel": ch, "state": state})

        elif path == "/api/clear":
            with lock:
                cleared = len(tasks)
                tasks.clear()
                task_events.clear()
                self._json({"cleared": cleared})

        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        # Suppress default access logging (noisy with extension polling)
        pass


# ── Logging ──────────────────────────────────────────────────────────────────

LOG_FILE = os.path.expanduser("~/.ask-gpt-git/server.log")


def _log(fmt, *args):
    msg = "[%s] %s" % (time.strftime("%H:%M:%S"), fmt % args if args else fmt)
    sys.stderr.write(msg + "\n")
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(msg + "\n")
    except Exception:
        pass


# ── Server ───────────────────────────────────────────────────────────────────

class ReusableServer(ThreadingHTTPServer):
    allow_reuse_address = True
    allow_reuse_port = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8801

    # Start the git-drop polling thread
    threading.Thread(
        target=gitdrop_advance_loop,
        args=(tasks, task_events, lock, apply_result),
        daemon=True,
        name="gitdrop-advance",
    ).start()

    srv = ReusableServer(("0.0.0.0", port), Handler)
    _log("ask-gpt-git server on :%d", port)
    print("ask-gpt-git server on :%d (git-drop only)" % port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        srv.server_close()


if __name__ == "__main__":
    main()
