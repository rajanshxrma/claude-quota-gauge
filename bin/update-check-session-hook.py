#!/usr/bin/env python3
"""SessionStart hook: checks once a day whether a newer claude-quota-gauge
is available, so an install never silently drifts behind fixes shipped
upstream -- found live (2026-08-06): a real correctness bug (the Fable
weekly-estimate calibration overwriting its derived cap outright on every
read, instead of smoothing against history, letting one noisy sample swing
the displayed % by ~60%) sat fixed in one install for a while with no way
for any other install to learn about it short of manually re-cloning.

Two tiers, both off unless a newer version actually exists:

  - Default: notify only. Injects a one-line heads-up naming the current
    and available version and the exact update command -- never touches
    a file on disk. This is the tier every install gets for free, no
    config needed.
  - Opt-in (CLAUDE_QUOTA_GAUGE_AUTO_UPDATE=1): actually applies the
    update -- re-downloads every file this same hook would install fresh
    (bin/*.py, commands/*.md) straight from the pinned repo/branch below,
    and updates the local version marker. Still always says what it did
    in the injected context -- auto-applying is never silent, even when
    opted in, on the same principle as everything else this tool surfaces
    ambiently: a background change to files that run with real permissions
    (hooks execute on every prompt) should never be invisible to the
    person running it.

Deliberately NOT configurable which repo/branch this pulls from -- that's
a fixed constant below, not an env var, so a stray misconfigured env var
can never repoint this at an arbitrary untrusted source. If you fork this
project, change the constants in your fork; don't make them runtime-
configurable in yours either, for the same reason.

Every network call here has a short timeout and fails silent (no context
injected, nothing written) on any error -- an update check must never be
the reason a session start hangs or a hook errors out for someone whose
network is offline or flaky.
"""
import sys, os, json, subprocess
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

REPO_OWNER = "rajanshxrma"
REPO_NAME = "claude-quota-gauge"
REPO_BRANCH = "main"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{REPO_BRANCH}"
API_BASE = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents"

SCRIPTS = os.path.expanduser("~/.claude/scripts")
COMMANDS = os.path.expanduser("~/.claude/commands")
VERSION_MARKER = os.path.expanduser("~/.claude/claude-quota-gauge-version")
CHECK_STATE_PATH = os.path.join(SCRIPTS, "claude-quota-gauge-update-check.json")
NETWORK_TIMEOUT = 4  # seconds -- a session start should never visibly hang on this
CHECK_INTERVAL_HOURS = float(os.environ.get("CLAUDE_QUOTA_GAUGE_UPDATE_CHECK_HOURS", "24"))
AUTO_UPDATE = os.environ.get("CLAUDE_QUOTA_GAUGE_AUTO_UPDATE") == "1"
UA = {"User-Agent": f"{REPO_NAME}-update-check"}


def _fetch_text(url):
    req = Request(url, headers=UA)
    with urlopen(req, timeout=NETWORK_TIMEOUT) as resp:
        return resp.read().decode("utf-8")


def _parse_version(v):
    try:
        return tuple(int(p) for p in v.strip().split("."))
    except Exception:
        return None


def _load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def _should_check(state, now):
    last = state.get("last_checked_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except Exception:
        return True
    return now - last_dt > timedelta(hours=CHECK_INTERVAL_HOURS)


def _apply_update():
    """Re-downloads every bin/*.py and commands/*.md from the pinned repo
    and overwrites the local install. Only ever called when
    CLAUDE_QUOTA_GAUGE_AUTO_UPDATE=1. Listing via the GitHub API (not a
    hardcoded filename list) so a new file added upstream gets picked up
    here with zero maintenance on this hook. Downloads into temp names
    first and only renames over the real target once every fetch in the
    batch has succeeded -- a network failure partway through must never
    leave a half-updated, mismatched set of scripts installed."""
    fetched = {}
    for subdir, dest_dir, exts in (("bin", SCRIPTS, (".py",)), ("commands", COMMANDS, (".md",))):
        listing = json.loads(_fetch_text(f"{API_BASE}/{subdir}?ref={REPO_BRANCH}"))
        for entry in listing:
            name = entry["name"]
            if not name.endswith(exts):
                continue
            content = _fetch_text(f"{RAW_BASE}/{subdir}/{name}")
            if not content.strip():
                raise ValueError(f"empty content fetched for {subdir}/{name}, aborting update")
            fetched[os.path.join(dest_dir, name)] = content

    for dest_path, content in fetched.items():
        tmp_path = dest_path + ".update-tmp"
        with open(tmp_path, "w") as f:
            f.write(content)
        os.chmod(tmp_path, 0o755)
        os.replace(tmp_path, dest_path)

    return len(fetched)


def main():
    now = datetime.now(timezone.utc)
    state = _load_json(CHECK_STATE_PATH, {})

    if not _should_check(state, now):
        return

    # Record the attempt regardless of outcome, so a persistent network
    # failure doesn't turn into a check-every-single-prompt retry storm --
    # it just tries again after the same interval, like a normal miss.
    state["last_checked_at"] = now.isoformat()
    try:
        with open(CHECK_STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass

    if not os.path.exists(VERSION_MARKER):
        return  # not installed via install.sh (or a pre-update-check install) -- nothing to compare against
    with open(VERSION_MARKER) as f:
        local_version = f.read().strip()

    try:
        remote_version = _fetch_text(f"{RAW_BASE}/VERSION").strip()
    except (URLError, HTTPError, TimeoutError, Exception):
        return  # offline or GitHub unreachable -- silent miss, retried next interval

    local_t, remote_t = _parse_version(local_version), _parse_version(remote_version)
    if not local_t or not remote_t or remote_t <= local_t:
        return

    if AUTO_UPDATE:
        try:
            n = _apply_update()
        except Exception as e:
            context = (
                f"claude-quota-gauge {remote_version} is available (you're on "
                f"{local_version}) -- auto-update is on but the update failed "
                f"({e}). Update by hand: cd into your clone, `git pull`, then "
                f"`./install.sh`."
            )
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}))
            return
        with open(VERSION_MARKER, "w") as f:
            f.write(remote_version)
        context = (
            f"claude-quota-gauge auto-updated {local_version} -> {remote_version} "
            f"({n} files) -- see CHANGELOG.md in your clone (or "
            f"https://github.com/{REPO_OWNER}/{REPO_NAME}/blob/main/CHANGELOG.md) "
            f"for what changed. Nothing else needed."
        )
    else:
        context = (
            f"claude-quota-gauge {remote_version} is available (you're on "
            f"{local_version}). Update: cd into your clone, `git pull`, then "
            f"`./install.sh` -- or set CLAUDE_QUOTA_GAUGE_AUTO_UPDATE=1 in "
            f"~/.claude/claude-quota-gauge.env to apply future updates "
            f"automatically (still always announced here, never silent)."
        )

    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}))


if __name__ == "__main__":
    main()
