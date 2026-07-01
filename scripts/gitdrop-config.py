#!/usr/bin/env python3
"""gitdrop-config — view/edit the channel-prefix -> repo mapping that ask-gpt.py uses
to auto-fill every git-drop instruction. This IS the mechanical mapping: a channel
named <prefix><digits> (dm1, chan2, cron3, …) resolves by longest-prefix to a repo,
and ask-gpt.py writes that repo/branch/file into the prompt itself — the LLM never
types a repo. Edit this when you start/retire a project.

Usage:
  gitdrop-config.py                 # show the mapping + sample channel resolutions
  gitdrop-config.py check           # validate every rule via gh (repo/branch/drop file)
  gitdrop-config.py add <prefix> <owner/repo> <branch> [file-pattern]
                                    # add/replace a rule (file-pattern default
                                    #   scratch/_CHATGPT_DROP_{ch}.md); validates + can
                                    #   create the drop file
  gitdrop-config.py rm <prefix>     # remove a rule
  gitdrop-config.py resolve <chan>  # show which repo a given channel name maps to
  gitdrop-config.py on | off        # enable/disable git-drop globally
"""
import json, os, sys, subprocess

DEFAULT_CFG = "~/repos/ask-gpt-git/config/channel-routes.json"
LEGACY_CFG = "~/.openclaw/workspace/scripts/gitdrop-targets.json"
CFG = os.path.expanduser(os.environ.get("ASK_GITDROP_CONFIG", DEFAULT_CFG))
DEFAULT_FILE = "scratch/_CHATGPT_DROP_{ch}.md"


def load():
    with open(CFG) as f:
        return json.load(f)


def save(cfg):
    with open(CFG, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def rule_for(cfg, ch):
    for r in sorted(cfg.get("rules", []), key=lambda r: -len(r.get("prefix", ""))):
        if ch.startswith(r.get("prefix", "\0")):
            return r
    return None


def gh_ok(args):
    try:
        return subprocess.run(["gh", "api"] + args, capture_output=True, timeout=15,
                              text=True).returncode == 0
    except Exception:
        return False


def cmd_show():
    cfg = load()
    print("git-drop mapping  (enabled=%s)  %s" % (cfg.get("enabled", True), CFG))
    print("  %-10s %-38s %-14s %-34s %s" % ("PREFIX", "REPO", "BRANCH", "FILE", "WORKTREE"))
    print("  " + "-" * 128)
    for r in sorted(cfg.get("rules", []), key=lambda r: r.get("prefix", "")):
        print("  %-10s %-38s %-14s %-34s %s" % (
            r["prefix"]+"*", r["repo"], r["branch"], r["file"], r.get("worktree", "")))
    print("\n  sample resolutions (channel name -> repo):")
    for ch in ("Q1", "ccc2", "shen1", "ripple2", "BGP1", "dm1"):
        r = rule_for(cfg, ch)
        print("    %-9s -> %s" % (ch, (r["repo"]+"@"+r["branch"]) if r else "(no rule — git-drop OFF for this channel)"))
    print("\n  naming convention: a channel is <prefix><digits>; the prefix picks the repo.")


def cmd_resolve(ch):
    cfg = load(); r = rule_for(cfg, ch)
    if not r:
        print("%s -> NO RULE (git-drop disabled for this channel; add one with `add`)" % ch); return
    print("%s -> %s@%s : %s" % (ch, r["repo"], r["branch"], r["file"].replace("{ch}", ch)))
    if r.get("worktree"):
        print("worktree: %s" % r["worktree"])


def cmd_add(prefix, repo, branch, filepat=None):
    filepat = filepat or DEFAULT_FILE
    if "/" not in repo:
        print("repo must be owner/repo, e.g. xiangyazi24/FLT"); sys.exit(2)
    print("validating %s @ %s …" % (repo, branch))
    if not gh_ok(["repos/%s" % repo, "--jq", ".name"]):
        print("  ✗ repo not reachable via gh: %s" % repo); sys.exit(1)
    if not gh_ok(["repos/%s/branches/%s" % (repo, branch), "--jq", ".name"]):
        print("  ⚠ branch '%s' not found on %s — create it first (gh/git) or fix the name." % (branch, repo)); sys.exit(1)
    print("  ✓ repo + branch exist")
    cfg = load()
    cfg.setdefault("rules", [])
    cfg["rules"] = [r for r in cfg["rules"] if r.get("prefix") != prefix]
    cfg["rules"].append({"prefix": prefix, "repo": repo, "branch": branch, "file": filepat})
    save(cfg)
    print("  ✓ rule added: %s* -> %s@%s : %s" % (prefix, repo, branch, filepat))
    print("  (drop files are auto-created on first use, or pre-create with gitdrop-config.py check)")


def cmd_rm(prefix):
    cfg = load()
    before = len(cfg.get("rules", []))
    cfg["rules"] = [r for r in cfg.get("rules", []) if r.get("prefix") != prefix]
    save(cfg)
    print("removed %d rule(s) for prefix '%s'" % (before - len(cfg["rules"]), prefix))


def cmd_toggle(on):
    cfg = load(); cfg["enabled"] = on; save(cfg)
    print("git-drop globally", "ENABLED" if on else "DISABLED")


def cmd_check():
    cfg = load(); bad = 0
    print("checking every rule via gh …")
    for r in sorted(cfg.get("rules", []), key=lambda r: r.get("prefix", "")):
        repo, br = r["repo"], r["branch"]
        repo_ok = gh_ok(["repos/%s" % repo, "--jq", ".name"])
        br_ok = repo_ok and gh_ok(["repos/%s/branches/%s" % (repo, br), "--jq", ".name"])
        wt = os.path.expanduser(r.get("worktree", "") or "")
        wt_note = ""
        if wt:
            wt_note = "  worktree:%s" % ("ok" if os.path.isdir(wt) else "missing")
        flag = "✓" if br_ok else "✗"
        print("  %s %-10s %s@%s%s%s" % (flag, r["prefix"]+"*", repo, br,
              "" if br_ok else ("  (repo missing)" if not repo_ok else "  (branch missing)"),
              wt_note))
        bad += 0 if br_ok else 1
    print("  all good" if not bad else "  %d rule(s) need attention" % bad)


def main():
    a = sys.argv[1:]
    if not a or a[0] in ("show", "list", "ls"):
        cmd_show()
    elif a[0] == "check":
        cmd_check()
    elif a[0] == "resolve" and len(a) == 2:
        cmd_resolve(a[1])
    elif a[0] == "add" and len(a) >= 4:
        cmd_add(a[1], a[2], a[3], a[4] if len(a) > 4 else None)
    elif a[0] == "rm" and len(a) == 2:
        cmd_rm(a[1])
    elif a[0] == "on":
        cmd_toggle(True)
    elif a[0] == "off":
        cmd_toggle(False)
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
