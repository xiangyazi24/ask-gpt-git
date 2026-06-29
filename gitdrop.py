#!/usr/bin/env python3
"""Git-drop polling logic.

Polls GitHub for new commits on the drop file. When a new commit lands
(SHA differs from the baseline stored on the task), marks the task as
completed. This is the server-side complement to ask-gpt.py's own
commit-poll — if ask-gpt.py dies or disconnects, the server still
detects completion.

The loop runs as a daemon thread started by server.py.
"""

import subprocess
import sys
import time


# How often to poll (seconds)
POLL_INTERVAL = 20

# Only poll tasks that have been processing for at least this long (seconds).
# Avoids hitting GitHub API on tasks that were just dispatched.
MIN_PROCESSING_AGE = 45


def _get_latest_commit(repo, branch, filepath):
    """Query GitHub API for the latest commit SHA touching a file.

    Uses `gh api` (GitHub CLI) so we inherit the user's auth without
    managing tokens ourselves.

    Returns the commit SHA as a string, or '' on failure.
    """
    try:
        rc = subprocess.run(
            ["gh", "api",
             "repos/%s/commits?sha=%s&path=%s&per_page=1" % (repo, branch, filepath),
             "--jq", ".[0].sha"],
            capture_output=True, timeout=15, text=True)
        return rc.stdout.strip() if rc.returncode == 0 else ""
    except Exception:
        return ""


def gitdrop_advance_loop(tasks, task_events, lock, apply_result):
    """Main polling loop. Called as a thread target by server.py.

    Args:
        tasks: the shared task dict (task_id -> task)
        task_events: the shared event dict (task_id -> threading.Event)
        lock: the shared threading.Lock
        apply_result: function(tid, answer, provenance) to mark a task completed
    """
    while True:
        try:
            now = time.time()
            # Snapshot the tasks we need to poll (under lock)
            todo = []
            with lock:
                for t in tasks.values():
                    gd = t.get("gitdrop")
                    if not gd:
                        continue
                    if t.get("status") != "processing":
                        continue
                    age = now - t.get("updated", t.get("created", now))
                    if age < MIN_PROCESSING_AGE:
                        continue
                    todo.append((t["id"], t.get("channel", ""), dict(gd)))

            # Poll each task (outside the lock — network I/O)
            for tid, ch, gd in todo:
                repo = gd.get("repo")
                branch = gd.get("branch")
                filepath = gd.get("file")
                baseline = gd.get("baseline", "")
                if not (repo and branch and filepath):
                    continue

                cur = _get_latest_commit(repo, branch, filepath)

                if cur and not baseline:
                    # Empty baseline (gh blip at dispatch time). Learn it from
                    # the first successful poll so we never falsely match the
                    # OLD commit as the answer.
                    with lock:
                        t = tasks.get(tid)
                        if t is not None and isinstance(t.get("gitdrop"), dict):
                            t["gitdrop"]["baseline"] = cur
                    continue

                if cur and cur != baseline:
                    # New commit detected — the answer landed.
                    with lock:
                        t = tasks.get(tid)
                        if t is not None and t.get("status") == "processing":
                            apply_result(
                                tid,
                                "[git-drop] committed %s — answer is in the repo." % cur,
                                provenance="gitdrop-server")
                            sys.stderr.write(
                                "[%s] gitdrop-advance %s %s %s\n"
                                % (time.strftime("%H:%M:%S"), ch,
                                   tid[:8], cur[:9]))

        except Exception as e:
            sys.stderr.write("[gitdrop-advance] loop error: %s\n" % type(e).__name__)

        time.sleep(POLL_INTERVAL)
