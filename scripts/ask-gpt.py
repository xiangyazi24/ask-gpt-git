#!/usr/bin/env python3
"""ask-gpt.py <question...> — ask ChatGPT (with the current tmux window's channel
group and connected GitHub repo) via the chatgpt-bridge on :8801. Default delivery
is git-drop-only: the authoritative answer is a commit to the configured drop file,
not DOM capture. Handles long Pro-extended answers (loops /api/wait up to a 45-min
overall deadline)."""
import sys, json, time, os, urllib.request, urllib.error, subprocess

def _nudge_caller(channel, msg):
    """Inject a notification directly into the calling tmux window.
    No nudge file, no watchdog — direct tmux send-keys."""
    # Determine calling tmux window
    win = ""
    try:
        win = subprocess.run(
            ["tmux", "display-message", "-p", "-t",
             os.environ.get("TMUX_PANE", ""), "#W"],
            capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        pass
    if not win:
        # Fallback: derive from channel prefix
        prefix = channel.rstrip("0123456789")
        win = prefix.replace("-work", "")
    if not win:
        return
    # Inject via tmux send-keys (spawned so it doesn't block exit)
    try:
        subprocess.Popen(
            ["bash", "-c",
             'sleep 3; tmux send-keys -t "zinan:%s" "⚡ %s" Enter; sleep 1; tmux send-keys -t "zinan:%s" Enter'
             % (win, msg.replace('"', '\\"'), win)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except Exception:
        pass


def _http(path, obj=None, to=620):
    url = "http://localhost:8801" + path
    if obj is None:
        return json.load(urllib.request.urlopen(url, timeout=to))
    data = json.dumps(obj).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))
if len(sys.argv) < 2:
    print("usage: ask-gpt.py <question...>")
    sys.exit(2)

# ── Auto-dispatch: detect tmux window → find idle channel from bridge ──────
def _reserve_path():
    return os.path.expanduser(os.environ.get(
        "ASK_CHANNEL_RESERVE_PATH",
        "~/.chatgpt-bridge/channel-reserve.json"))

def _with_reserve_lock(fn):
    import fcntl
    path = _reserve_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        pass
    lk = None
    try:
        lk = open(path + ".lock", "w")
        fcntl.flock(lk, fcntl.LOCK_EX)
        return fn(path)
    finally:
        if lk:
            try:
                fcntl.flock(lk, fcntl.LOCK_UN)
                lk.close()
            except Exception:
                pass

def _load_reservations(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _store_reservations(path, reservations):
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w") as f:
        json.dump(reservations, f, sort_keys=True)
    os.replace(tmp, path)

def _reserve_channel(channels, by_ch):
    """Atomically reserve one bridge channel for this process.

    Bridge /api/status is not an atomic allocator: parallel ask-gpt.py processes
    can all observe the same idle channel before any /api/ask marks it busy. This
    short local reservation closes that race until /api/ask returns.
    """
    token = "%d:%f" % (os.getpid(), time.time())
    ttl = 300.0
    try:
        ttl = max(10.0, float(os.environ.get("ASK_CHANNEL_RESERVE_TTL", "300")))
    except Exception:
        pass
    now = time.time()

    def choose(path):
        reservations = _load_reservations(path)
        live = {}
        for res_ch, r in reservations.items():
            if not isinstance(r, dict):
                continue
            try:
                expires = float(r.get("expires", 0))
            except Exception:
                continue
            if expires > now:
                live[res_ch] = r
        reservations = live

        def reserved(ch):
            return ch in reservations

        def load(ch):
            info = by_ch.get(ch, {})
            return int(info.get("processing", 0) or 0) + int(info.get("pending", 0) or 0)

        chosen = None
        for ch in channels:
            if not reserved(ch) and load(ch) == 0:
                chosen = ch
                break
        if chosen is None:
            unreserved = [ch for ch in channels if not reserved(ch)]
            pool = unreserved if unreserved else list(channels)
            chosen = min(pool, key=load)

        reservations[chosen] = {"token": token, "expires": now + ttl}
        _store_reservations(path, reservations)
        return chosen, token

    return _with_reserve_lock(choose)

def _release_channel_reservation(channel, token):
    if not channel or not token:
        return
    now = time.time()

    def release(path):
        reservations = _load_reservations(path)
        changed = False
        out = {}
        for ch, r in reservations.items():
            try:
                expires = float(r.get("expires", 0)) if isinstance(r, dict) else 0.0
            except Exception:
                expires = 0.0
            if expires <= now:
                changed = True
                continue
            if ch == channel and r.get("token") == token:
                changed = True
                continue
            out[ch] = r
        if changed:
            _store_reservations(path, out)
        return None

    try:
        _with_reserve_lock(release)
    except Exception:
        pass

def _auto_channel():
    """Find an idle channel in the current tmux window's group."""
    import subprocess
    try:
        win = subprocess.run(
            ["tmux", "display-message", "-p", "-t", os.environ.get("TMUX_PANE", ""), "#W"],
            capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        win = ""
    if not win:
        return None, None
    prefix = win.replace("-work", "")  # chan-work → chan
    try:
        status = json.load(urllib.request.urlopen("http://localhost:8801/api/status", timeout=5))
    except Exception:
        return None, win
    groups = status.get("groups", {})
    channels = groups.get(prefix, [])
    if not channels:
        by_ch = status.get("by_channel", {})
        channels = sorted([c for c in by_ch if c.startswith(prefix)])
    if not channels:
        return None, win
    by_ch = status.get("by_channel", {})
    try:
        ch, token = _reserve_channel(channels, by_ch)
    except Exception:
        token = ""
        ch = None
        for cand in channels:
            info = by_ch.get(cand, {})
            if info.get("processing", 0) == 0 and info.get("pending", 0) == 0:
                ch = cand
                break
        if ch is None:
            ch = channels[0]
    info = by_ch.get(ch, {})
    if info.get("processing", 0) or info.get("pending", 0):
        sys.stderr.write("[ask-gpt] all channels in group '%s' busy, queuing on %s\n" % (prefix, ch))
        sys.stderr.flush()
    return ch, win, token

# Always auto-dispatch. Question is all args (or stdin).
q = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
if not q:
    q = sys.stdin.read()
_auto = _auto_channel()
if len(_auto) == 3:
    ch, _win, _chan_res_token = _auto
else:
    ch, _win = _auto
    _chan_res_token = ""
if not ch:
    print("[BRIDGE: no channels for window '%s' — is the bridge running?]" % (_win or "unknown"))
    sys.exit(2)
sys.stderr.write("[ask-gpt] → %s (window: %s)\n" % (ch, _win))
sys.stderr.flush()

q = q.strip()
if not q:
    _release_channel_reservation(ch, _chan_res_token)
    print("[BRIDGE: empty question]"); sys.exit(2)
# ── HUMAN-readable numbered ledger (mechanized) ──────────────────────────────
# The caller LLM used to (a) number each question Q<N>, (b) prefix the brief so
# the number shows in the tab, (c) keep a persistent list, (d) update status on
# return — and kept forgetting. All of that is now CODE here: it happens on every
# call regardless of which session/LLM invoked the script.
import fcntl, re
LEDGER = os.path.expanduser(os.environ.get("ASK_LEDGER_PATH", "~/.chatgpt-bridge/ASK_LEDGER.md"))
_HEADER = ("# ChatGPT ASK LEDGER\n"
           "Status: ⏳ dispatched · ✅ captured (>500B, no [BRIDGE:) · ✗ NEEDS-PASTE · ↩ recovered\n"
           "<!-- next Q#: 1 -->\n\n"
           "| Q# | time | channel | topic | status | RUN# |\n"
           "|----|------|---------|-------|--------|------|\n")

def _summarize(text):
    # Human handle for the question. The LLM MAY pass a better one via $ASK_LABEL
    # but never has to — the handle always exists without it remembering.
    lab = os.environ.get("ASK_LABEL", "").strip()
    if lab:
        return (lab[:72] + "…") if len(lab) > 72 else lab
    for ln in text.splitlines():
        ln = ln.strip()
        if ln:
            return (ln[:70] + "…") if len(ln) > 70 else ln
    return "(empty)"

def _ledger_io(fn):
    # Every mutation under an exclusive lock, so concurrent ask-gpt.py processes
    # (parallel per-channel dispatch) never collide on the Q# cursor — the exact
    # failure that produced duplicate Q7/Q8 rows under hand-maintenance.
    try:
        os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    except Exception:
        pass
    try:
        lk = open(LEDGER + ".lock", "w")
        fcntl.flock(lk, fcntl.LOCK_EX)
    except Exception:
        lk = None
    try:
        body = open(LEDGER).read() if os.path.exists(LEDGER) else _HEADER
        out, ret = fn(body)
        with open(LEDGER, "w") as f:
            f.write(out)
        return ret
    except Exception:
        return None
    finally:
        if lk:
            try: fcntl.flock(lk, fcntl.LOCK_UN); lk.close()
            except Exception: pass

def _ledger_assign(channel, topic):
    def fn(body):
        if "<!-- next Q#:" not in body:
            body = _HEADER + body if not body.startswith("#") else body
        m = re.search(r"<!-- next Q#: (\d+) -->", body)
        n = int(m.group(1)) if m else 1
        body = re.sub(r"<!-- next Q#: \d+ -->", "<!-- next Q#: %d -->" % (n + 1), body, count=1)
        row = "| Q%d | %s | %s | %s | ⏳ dispatched | — |\n" % (
            n, time.strftime("%H:%M"), channel, topic.replace("|", "/"))
        return body.rstrip("\n") + "\n" + row, n
    return _ledger_io(fn)

def _ledger_update(n, status, run):
    def fn(body):
        pat = r"\| Q%d \| (?P<t>[^|]*)\| (?P<c>[^|]*)\| (?P<topic>[^|]*)\| [^|]*\| [^|]*\|" % n
        def repl(mo):
            return "| Q%d | %s| %s| %s| %s | %s |" % (
                n, mo.group("t"), mo.group("c"), mo.group("topic"), status, run)
        return re.subn(pat, repl, body, count=1)[0], None
    return _ledger_io(fn)

summary = _summarize(q)
qnum = _ledger_assign(ch, summary) or 0

# ── git-drop DEFAULT (2026-06-23): route every substantive answer to a
# byte-perfect GIT COMMIT instead of relying on fragile DOM scraping. Auto-append
# a git-drop instruction using the channel's configured target. In git-drop-only
# mode, missing target = hard failure; DOM fallback is only for explicit bridge
# debugging with ASK_ALLOW_DOM=1.
def _gitdrop_target(channel):
    """Return {repo,branch,file} for this channel's git-drop, or None."""
    if os.environ.get("ASK_NO_GITDROP", "").strip() in ("1", "true", "yes"):
        return None
    cfgp = os.path.expanduser(os.environ.get(
        "ASK_GITDROP_CONFIG",
        "~/repos/ask-gpt-git/config/channel-routes.json"))
    if not os.path.exists(cfgp):
        cfgp = os.path.expanduser("~/.openclaw/workspace/scripts/gitdrop-targets.json")
    try:
        cfg = json.load(open(cfgp))
    except Exception:
        return None
    if not cfg.get("enabled", True):
        return None
    for r in sorted(cfg.get("rules", []), key=lambda r: -len(r.get("prefix", ""))):
        if channel.startswith(r.get("prefix", "\0")):
            return {"repo": r["repo"], "branch": r["branch"],
                    "file": r["file"].replace("{ch}", channel),
                    "worktree": r.get("worktree", "")}
    return None

def _gitdrop_instruction(channel, question, tgt):
    if not tgt:
        return ""
    return ("\n\n----\nIMPORTANT (git-drop): write your COMPLETE response into "
            "`%s` on the `%s` branch of `%s` via the GitHub connector — UPDATE the "
            "existing file (create it if absent), overwriting its contents. Use "
            "markdown: prose for explanation/reasoning, code in ```lang fenced "
            "blocks with all imports. After committing, report the commit SHA.\n"
            "DELIVERY RULES (strict): the ONLY acceptable delivery is a real Git "
            "COMMIT to that GitHub repo via the connector. Do NOT use the Python / "
            "code-interpreter / sandbox tool, do NOT write to `/mnt/data`, and do NOT "
            "give a `sandbox:` download link — a sandbox file is NOT a commit and we "
            "cannot read it. If the GitHub connector is unavailable or read-only, say "
            "so explicitly (write `GIT-DROP FAILED: connector unavailable`) instead of "
            "falling back to a sandbox file. Success = a commit SHA on %s."
            % (tgt["file"], tgt["branch"], tgt["repo"], tgt["repo"]))

def _drop_head_sha(tgt):
    """Latest commit SHA touching the drop file (gh), or '' — the git-drop
    completion signal, fully DECOUPLED from DOM capture/finalize/color."""
    if not tgt:
        return ""
    try:
        import subprocess
        rc = subprocess.run(
            ["gh", "api", "repos/%s/commits?sha=%s&path=%s&per_page=1"
             % (tgt["repo"], tgt["branch"], tgt["file"]),
             "--jq", ".[0].sha"], capture_output=True, timeout=15, text=True)
        return rc.stdout.strip() if rc.returncode == 0 else ""
    except Exception:
        return ""

_gdtgt = _gitdrop_target(ch)
_allow_dom_fallback = os.environ.get("ASK_ALLOW_DOM", "").strip().lower() in ("1", "true", "yes")
if not _gdtgt and not _allow_dom_fallback:
    _release_channel_reservation(ch, _chan_res_token)
    if qnum:
        _ledger_update(qnum, "✗ no git-drop target", "—")
    print("[BRIDGE: no git-drop target for channel '%s'. Add a rule with gitdrop-config.py; DOM fallback is disabled by default. For bridge debugging only, set ASK_ALLOW_DOM=1.]" % ch)
    sys.exit(2)

# ── Auto-push local changes so ChatGPT sees the latest code ──────────────────
# Only push if: (1) git-drop is configured, (2) we're inside a git repo,
# (3) there are commits ahead of the remote. Skip on ASK_NO_PUSH=1.
_push_sha = ""
if _gdtgt and not os.environ.get("ASK_NO_PUSH", "").strip() in ("1", "true"):
    try:
        # Find the git repo root (we might be in a subdirectory)
        _repo_root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5).stdout.strip()
        _expected_worktree = os.path.expanduser(_gdtgt.get("worktree", "") or "")
        if _repo_root and _expected_worktree:
            if os.path.realpath(_repo_root) != os.path.realpath(_expected_worktree):
                sys.stderr.write(
                    "[ask-gpt] auto-push skipped: cwd repo %s != route worktree %s\n"
                    % (_repo_root, _expected_worktree))
                sys.stderr.flush()
                _repo_root = ""
        if _repo_root:
            # Check if there are unpushed commits
            _ahead = subprocess.run(
                ["git", "-C", _repo_root, "rev-list", "--count", "@{u}..HEAD"],
                capture_output=True, text=True, timeout=5).stdout.strip()
            if _ahead and int(_ahead) > 0:
                _push_rc = subprocess.run(
                    ["git", "-C", _repo_root, "push"],
                    capture_output=True, text=True, timeout=30)
                if _push_rc.returncode == 0:
                    _push_sha = subprocess.run(
                        ["git", "-C", _repo_root, "rev-parse", "--short", "HEAD"],
                        capture_output=True, text=True, timeout=5).stdout.strip()
                    sys.stderr.write("[ask-gpt] auto-pushed %s commits (HEAD=%s)\n" % (_ahead, _push_sha))
                    sys.stderr.flush()
    except Exception:
        pass

q = q + _gitdrop_instruction(ch, q, _gdtgt)

# If we pushed, tell ChatGPT what commit to look at
if _push_sha and _gdtgt:
    q += "\n\nNOTE: The repo has been updated. Look at the latest pushed commit (%s) on the current branch for the current state of the code." % _push_sha

# Baseline = commit BEFORE ChatGPT writes. Critical for correctness: an empty
# baseline makes the commit-poll match the existing OLD commit and falsely report
# success before the question is even answered. Retry a few times to avoid a gh
# blip leaving it empty; the poll loops still learn-on-first-poll as a backstop.
_gd_baseline = ""
if _gdtgt:
    for _ in range(3):
        _gd_baseline = _drop_head_sha(_gdtgt)
        if _gd_baseline:
            break
        time.sleep(2)
# Prefix the brief so the number shows IN the ChatGPT tab — that is how Xiang
# matches a tab to its ledger row (documented design). Was hand-done & forgotten.
q_sent = "Q%d (%s): %s" % (qnum, ch, q) if qnum else q
def _ask_with_retry(payload, attempts=6, backoff=4):
    """The initial /api/ask had NO retry — an ask sent during a bridge restart/blip was
    silently lost (UNDERSTANDING RUN#12). Retry a few times (≈25s window, covers a
    launchctl bootout/bootstrap) so the dispatch call is robust. Normal path returns
    on the first try, unchanged."""
    for i in range(attempts):
        try:
            t = _http("/api/ask", payload).get("id")
            if t:
                return t
            reason = "no-id"
        except Exception as e:
            reason = type(e).__name__
        if i < attempts - 1:
            sys.stderr.write("[ask-gpt] /api/ask %s — retry %d/%d in %ds\n"
                             % (reason, i + 1, attempts, backoff))
            sys.stderr.flush()
            time.sleep(backoff)
    return None
_ask_body = {"question": q_sent, "channel": ch}
if _gdtgt:
    # Hand the git-drop target + baseline SHA to the server so IT can poll the commit
    # and auto-advance a stuck/purple tab when the answer lands — not dependent on
    # this process staying alive.
    _ask_body["gitdrop"] = {"repo": _gdtgt["repo"], "branch": _gdtgt["branch"],
                            "file": _gdtgt["file"], "baseline": _gd_baseline or ""}
tid = _ask_with_retry(_ask_body)
_release_channel_reservation(ch, _chan_res_token)
if not tid:
    if qnum: _ledger_update(qnum, "✗ ask-failed", "—")
    print("[BRIDGE: ask failed]"); sys.exit(1)
deadline = time.time() + 2700  # 45 min overall; covers 20+ min Pro-extended
_start = time.time()
def _hb(msg):
    # Liveness to STDERR (never stdout — stdout is the captured answer).
    sys.stderr.write("[ask-gpt Q%d %s tid=%s +%ds] %s\n"
                     % (qnum, ch, tid, int(time.time() - _start), msg))
    sys.stderr.flush()
def _banner(headline):
    # Loud, HUMAN-led banner: Q# · channel · status · summary. Machine ids
    # (bridge task / harness bg id) demoted to a small debug tail — they never
    # headline a status line a person reads.
    sys.stderr.write('═══ BRIDGE Q%d →%s %s | "%s" | task %s ═══\n'
                     % (qnum, ch, headline, summary, tid))
    sys.stderr.flush()
r = {}
_gd_done = False        # git-drop completed via a new commit (DOM-independent)
_gd_commit = ""
_poll_to = 20 if _gdtgt else 90   # git-drop: tick fast so the commit-poll is timely
try:
    _terminal_commit_grace = max(0, int(os.environ.get("ASK_GITDROP_TERMINAL_GRACE", "180")))
except Exception:
    _terminal_commit_grace = 180
_terminal_seen_at = 0.0
_banner("SUBMITTED")
_hb("dispatched, waiting for answer" + (" (git-drop: watching commit)" if _gdtgt else ""))
while time.time() < deadline:
    # PRIMARY completion signal for git-drop = a NEW commit on the drop file.
    # Fully DECOUPLED from DOM capture / finalize / the purple busy-color: the
    # moment ChatGPT commits, we're done — no waiting on the bridge's DOM-driven
    # /api/wait status (which is what made the caller空等 after the answer landed).
    if _gdtgt:
        cur = _drop_head_sha(_gdtgt)
        # CORRECTNESS: if the baseline couldn't be read at dispatch (gh blip ->
        # empty), DON'T treat the existing OLD commit as "new" — that falsely
        # reports success before the question is even answered (observed chan*
        # 2026-06-25: GIT-DROP OK with no question asked). Learn the baseline from
        # the first poll instead, and only succeed on a commit that appears AFTER.
        if cur and not _gd_baseline:
            _gd_baseline = cur
            _hb("git-drop: learned baseline %s (was empty at dispatch)" % cur[:9])
        elif cur and cur != _gd_baseline:
            _gd_done = True; _gd_commit = cur
            _hb("git-drop commit landed %s — done (DOM-independent)" % cur[:9])
            # COMMIT-DRIVEN ADVANCE: tell the bridge the answer is delivered so the
            # tab stops its DOM wait and takes the NEXT task immediately — don't make
            # it hang on a phantom stop button / stuck finalize. Best-effort.
            try: _http("/api/gitdrop-done", {"task": tid, "channel": ch, "sha": cur})
            except Exception: pass
            break
    try:
        r = _http("/api/wait/%s?timeout=%d" % (tid, _poll_to))
    except urllib.error.HTTPError as he:
        # 408 = normal "not done yet"; its body carries the live status.
        if he.code == 408:
            try: r = json.load(he)
            except Exception: r = {}
        else:
            _hb("HTTP %s, retrying" % he.code); time.sleep(5); continue
    except Exception as e:
        _hb("server blip (%s), retrying" % type(e).__name__); time.sleep(5); continue
    st = r.get("status")
    if st in ("completed", "needs_manual") and _gdtgt and not _gd_done:
        if not _terminal_seen_at:
            _terminal_seen_at = time.time()
            _hb("terminal: status=%s but no git-drop commit yet; grace-polling" % st)
        if time.time() - _terminal_seen_at < _terminal_commit_grace:
            time.sleep(5)
            continue
    if st in ("completed", "failed", "needs_manual"):
        _hb("terminal: status=%s" % st); break
    _hb("waiting (status=%s%s)" % (st or "processing", " +commit-watch" if _gdtgt else ""))
a = r.get("answer")
if _gd_done:
    # Commit is authoritative; don't depend on whatever DOM capture produced.
    a = a or "[git-drop] answer committed; see the commit."
# git-drop detection: in git-drop mode the authoritative answer is the GIT COMMIT
# (ChatGPT writes the full response into the drop file and commits it); the chat
# reply is just a short "Committed. Commit SHA: <sha>" confirmation. Without this,
# that short reply trips the <500B TRUNCATED verdict and the run looks FAILED when
# it actually succeeded perfectly. Detect a commit-SHA confirmation and treat it
# as success (the commit is the source of truth, per the git-drop workflow).
_gitdrop_sha = ""
_gd_full = ""
if _gd_done and _gd_commit:
    _gd_full = _gd_commit                 # commit-poll: authoritative, DOM-independent
elif a:
    _gm = re.search(r"(?:commit\s*sha|committed|commit)[\s:`*]*\b([0-9a-f]{7,40})\b", a, re.I)
    if _gm:
        _gd_full = _gm.group(1)
if _gd_full:
    _gitdrop_sha = _gd_full[:9]
# Sandbox-instead-of-commit detection: ChatGPT sometimes writes the answer to its
# code-interpreter sandbox (`sandbox:/mnt/data/...`, a download link) instead of
# committing via the connector — NO commit lands, the answer is unreachable, and a
# git-drop run would otherwise just look "pending". Flag it loudly so the caller
# re-dispatches instead of waiting. Only when we expected a git-drop and none landed.
_gd_sandbox = bool(_gdtgt) and not _gitdrop_sha and bool(a) and bool(
    re.search(r"sandbox:|/mnt/data|download .*\.md|written it to a file", a, re.I))
_gd_no_commit = (bool(_gdtgt) and not _gitdrop_sha and not _gd_sandbox
                 and (bool(a) or r.get("status") in ("completed", "needs_manual"))
                 and not _allow_dom_fallback)
# Run ledger: one loud line per run in a shared log (run #, channel, task,
# bytes, verdict) so the operator can tell at a glance whether a silent
# stdin/stdout pipe actually succeeded — without digging into output files.
try:
    import os
    _log = os.path.expanduser("~/.chatgpt-bridge/runs.log")
    try:
        with open(_log) as f:
            _n = sum(1 for _ in f) + 1
    except FileNotFoundError:
        _n = 1
    # Verdict honesty: an empty capture is only a real FAIL when the bridge
    # itself reports status=="failed". status None/processing/needs_manual just
    # means THIS caller stopped waiting before the bridge finished — the bridge
    # parks long Pro-thinks and auto-requeues them, so the answer often lands in
    # the store (and/or the git-drop file) minutes later. Don't cry FAIL for that.
    _st = r.get("status")
    if _gitdrop_sha:
        _verdict = "GITDROP:%s" % _gitdrop_sha
    elif _gd_sandbox:
        _verdict = "GITDROP-SANDBOX-FAIL"
    elif _gd_no_commit:
        _verdict = "GITDROP-NO-COMMIT"
    elif a and len(a) > 500:
        _verdict = "OK"
    elif a:
        _verdict = "SHORT(%dB)" % len(a)
    elif _st == "failed":
        _verdict = "FAIL:failed"
    else:
        _verdict = "PENDING:%s" % (_st or "wait-timeout")
    with open(_log, "a") as f:
        f.write("RUN#%d %s ch=%s task=%s bytes=%d prov=%s verdict=%s\n" % (
            _n, time.strftime("%m-%d %H:%M:%S"), ch, tid,
            len(a or ""), r.get("provenance") or "?", _verdict))
except Exception:
    pass
# Result banner — printed unconditionally (verdict computed above), so the
# caller never has to read runs.log and reprint it by hand.
_el = int(time.time() - _start)
_elapsed = "%dm%02ds" % (_el // 60, _el % 60)
def _kb(n): return ("%.1fKB" % (n / 1024.0)) if n >= 1024 else ("%dB" % n)
_runref = "RUN#%d" % locals().get("_n", 0)
if _gitdrop_sha:
    _sym = "✅ GIT-DROP (commit %s, %s) — answer is in the commit, not this reply" % (_gitdrop_sha, _elapsed)
    _ledger_update(qnum, "✅ git-drop %s" % _gitdrop_sha, _runref)
elif _gd_sandbox:
    _sym = ("✗ GIT-DROP FAILED — ChatGPT wrote a SANDBOX file (/mnt/data, download "
            "link), NOT a commit. Answer is unreachable; RE-DISPATCH this question.")
    _ledger_update(qnum, "✗ sandbox-no-commit (re-dispatch)", _runref)
elif _gd_no_commit:
    _sym = ("✗ GIT-DROP FAILED — no commit landed. DOM reply ignored; check the "
            "GitHub connector/routing and re-dispatch.")
    _ledger_update(qnum, "✗ no-commit (re-dispatch)", _runref)
elif a and len(a) > 500:
    _sym = "✅ COMPLETE (%s, %s)" % (_kb(len(a)), _elapsed)
    _ledger_update(qnum, "✅ captured", _runref)
elif a:
    _sym = "✗ TRUNCATED (%s, %s) —需要手贴" % (_kb(len(a)), _elapsed)
    _ledger_update(qnum, "✗ NEEDS-PASTE", _runref)
elif r.get("status") == "failed":
    _sym = "✗ FAIL (failed)"
    _ledger_update(qnum, "✗ NEEDS-PASTE", _runref)
else:
    _sym = "✗ PENDING (%s) — 答案可能稍后落地" % (r.get("status") or "wait-timeout")
    _ledger_update(qnum, "⏳ pending", "—")
_banner(_sym)
if _gitdrop_sha:
    # DEDICATED git-drop success on stdout — one terse, gh-VERIFIED line the
    # calling LLM cannot misjudge. Two paths into here:
    #  (A) _gd_done: we POLLED the commit landing (DOM-independent) — target is
    #      known from config (_gdtgt) and the commit is verified by construction.
    #  (B) reply-SHA: parsed from the chat confirmation; verify it via gh + parse
    #      repo/branch/file from the reply.
    _full = _gd_full
    if _gd_done and _gdtgt:
        _verified = "VERIFIED"
        _tgt = " %s@%s:%s" % (_gdtgt["repo"], _gdtgt["branch"], _gdtgt["file"])
    else:
        def _grab(p):
            m = re.search(p, a or "", re.I); return m.group(1) if m else ""
        _gd_repo = _grab(r"repo[:\s`*]*([\w.-]+/[\w.-]+)")
        _gd_branch = _grab(r"branch[:\s`*]*`?([\w./-]+)")
        _gd_file = _grab(r"(?:file\s*path|path)[:\s`*]*`?([\w./_-]+\.\w+)")
        _verified = "reported"
        if _gd_repo and len(_full) >= 7:
            try:
                import subprocess
                _rc = subprocess.run(["gh", "api", "repos/%s/commits/%s" % (_gd_repo, _full),
                                      "--jq", ".sha"], capture_output=True, timeout=15, text=True)
                if _rc.returncode == 0 and _rc.stdout.strip().lower().startswith(_full.lower()[:7]):
                    _verified = "VERIFIED"
            except Exception:
                pass
        _tgt = ""
        if _gd_repo: _tgt += " %s" % _gd_repo
        if _gd_branch: _tgt += "@%s" % _gd_branch
        if _gd_file: _tgt += ":%s" % _gd_file
    # AUTO-EXTRACT the code so the caller can read CODE BY DEFAULT (cheaper tokens,
    # more direct) and only open the full .md for the prose/reasoning when it wants
    # to deepen understanding. Fetch the committed drop file, pull every fenced code
    # block, write the code to /tmp/gpt_<ch>.<ext> and the full markdown to
    # /tmp/gpt_<ch>.md. Best-effort; on any failure we just point at the repo.
    _code_path = _md_path = ""
    _ext = "txt"
    if _gd_done and _gdtgt:
        try:
            import subprocess
            _rc = subprocess.run(
                ["gh", "api", "repos/%s/contents/%s?ref=%s"
                 % (_gdtgt["repo"], _gdtgt["file"], _gdtgt["branch"]), "--jq", ".content"],
                capture_output=True, timeout=20, text=True)
            if _rc.returncode == 0 and _rc.stdout.strip():
                import base64
                _md = base64.b64decode(_rc.stdout.strip()).decode("utf-8", "replace")
                _md_path = "/tmp/gpt_%s.md" % ch
                open(_md_path, "w").write(_md)
                # pull fenced code blocks; remember the first language for the ext
                _blocks = re.findall(r"```([\w.-]*)\n(.*?)```", _md, re.S)
                if _blocks:
                    _langs = [b[0].lower() for b in _blocks if b[0]]
                    _lang = _langs[0] if _langs else ""
                    _ext = {"lean": "lean", "python": "py", "py": "py", "latex": "tex",
                            "tex": "tex", "haskell": "hs", "c": "c", "cpp": "cpp",
                            "rust": "rs", "javascript": "js", "typescript": "ts"}.get(_lang, "txt")
                    _code = "\n\n".join(b[1].rstrip() for b in _blocks)
                    _code_path = "/tmp/gpt_%s.%s" % (ch, _ext)
                    open(_code_path, "w").write(_code + "\n")
        except Exception:
            pass
    _hint = ""
    if _code_path:
        _hint = " | CODE→%s (read this; prose/reasoning in %s)" % (_code_path, _md_path)
    print("GIT-DROP OK [%s] %s%s%s" % (_verified, _gitdrop_sha, _tgt, _hint or
          " — answer is the commit, fetch from repo."), flush=True)
    _nudge_msg = "GIT-DROP OK %s" % _gitdrop_sha[:8]
    if _code_path:
        _nudge_msg += " — Read %s (code) or %s (full), then keep going" % (_code_path, _md_path)
    elif _md_path:
        _nudge_msg += " — Read %s, then keep going" % _md_path
    else:
        _nudge_msg += " — answer in commit, git fetch to read"
    _nudge_msg += ". 读答案，继续统筹滚动"
    _nudge_caller(ch, _nudge_msg)
    sys.exit(0)
elif _gd_no_commit:
    print("[BRIDGE: GIT-DROP FAILED — no commit landed; DOM reply ignored. Check GitHub connector/routing and re-dispatch.]", flush=True)
    _nudge_caller(ch, "ChatGPT %s GIT-DROP FAILED — no commit, re-dispatch" % ch)
    sys.exit(1)
elif a:
    print(a, flush=True)
    _nudge_caller(ch, "ChatGPT %s 答案到了(%s) — 读答案，继续统筹" % (ch, _kb(len(a))))
    sys.exit(0)
elif r.get("status") == "failed":
    print("[BRIDGE: FAILED status=failed prov=%s]" % r.get("provenance"), flush=True)
    _nudge_caller(ch, "ChatGPT %s FAILED — 检查并重发" % ch)
    sys.exit(1)
else:
    # Not a failure — caller stopped waiting before the bridge delivered.
    print("[BRIDGE: still PENDING (status=%s) — task %s not failed; "
          "answer may land in the git-drop file / bridge store shortly]"
          % (r.get("status"), tid), flush=True)
    sys.exit(0)
