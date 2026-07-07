#!/usr/bin/env python3
"""Estimates current session/weekly usage % by scaling local token deltas
against the last real calibration read from claude.ai/settings/usage.
Prints a compact human line to stdout, and (with --json) a machine-readable
summary for other scripts (e.g. the launchd usage-watch alert).
"""
import sys, os, shutil, json, subprocess
from datetime import datetime, timezone


def _load_env_file(path="~/.claude/usage-calibrator.env"):
    """Loads KEY=VALUE overrides so personal config works even when this
    script is invoked from a SessionStart hook or launchd -- neither
    inherits shell profile env vars. Never overrides an already-set var."""
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_env_file()

if os.environ.get("CCUSAGE"):
    CCUSAGE_CMD = [os.environ["CCUSAGE"]]
elif os.path.exists(os.path.expanduser("~/.npm-global/bin/ccusage")):
    CCUSAGE_CMD = [os.path.expanduser("~/.npm-global/bin/ccusage")]
elif shutil.which("ccusage"):
    CCUSAGE_CMD = [shutil.which("ccusage")]
else:
    CCUSAGE_CMD = ["npx", "ccusage@latest"]
SCRIPTS = os.path.expanduser("~/.claude/scripts")
CAL_PATH = os.path.join(SCRIPTS, "usage-calibration.json")
TRACK_MODEL = os.environ.get("CLAUDE_USAGE_TRACK_MODEL", "fable")


def _pending_file_path():
    if os.environ.get("CLAUDE_USAGE_PENDING_FILE"):
        return os.path.expanduser(os.environ["CLAUDE_USAGE_PENDING_FILE"])
    for candidate in ("./PENDING.md", "~/.claude/PENDING.md"):
        path = os.path.expanduser(candidate)
        if os.path.exists(path):
            return path
    return None


def pending_tasks_count():
    """Counts '## ' entries in PENDING.md -- each is one open item.
    Headings containing "RESOLVED" (case-insensitive) are kept in the file
    for reference but excluded from the count."""
    path = _pending_file_path()
    if not path or not os.path.exists(path):
        return None
    with open(path) as f:
        return sum(
            1 for line in f
            if line.startswith("## ") and "resolved" not in line.lower()
        )


def fmt_delta(iso_target, now):
    target = datetime.fromisoformat(iso_target.replace("Z", "+00:00"))
    delta = target - now
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "now"
    h, rem = divmod(secs, 3600)
    m = rem // 60
    if h > 24:
        d, h = divmod(h, 24)
        return f"{d}d {h}h"
    return f"{h}h {m}m"


def main():
    now = datetime.now(timezone.utc)

    if not os.path.exists(CAL_PATH):
        print("No calibration yet -- run /usage-recalibrate to read real % from claude.ai/settings/usage.")
        sys.exit(0)

    with open(CAL_PATH) as f:
        cal = json.load(f)

    result = {"calibrated_at": cal["calibrated_at"]}
    parts = []

    # --- session (5h block) ---
    block_data = json.loads(subprocess.check_output(CCUSAGE_CMD + ["blocks", "--active", "--json"]))
    blocks = block_data.get("blocks", [])
    sess = cal["session"]
    if blocks and blocks[0]["startTime"] == sess["block_start"]:
        cur_cost = blocks[0]["costUSD"]
        base_cost = max(sess["block_cost_at_cal"], 0.01)
        pct = sess["pct"] * (cur_cost / base_cost)
        pct = min(pct, 150)
        resets_in = fmt_delta(sess["block_end"], now)
        parts.append(f"5h: {pct:.0f}% (resets {resets_in})")
        result["session_pct"] = round(pct, 1)
        result["session_resets_at"] = sess["block_end"]
        result["session_stale"] = False
    elif blocks:
        # New block since calibration -- no baseline to scale against
        parts.append(f"5h: new block started, run /usage-recalibrate (resets {fmt_delta(blocks[0]['endTime'], now)})")
        result["session_stale"] = True
        result["session_new_block_start"] = blocks[0]["startTime"]
    else:
        parts.append("5h: no active block")
        result["session_stale"] = True

    # --- weekly ---
    wk = cal["weekly"]
    if now.isoformat() > wk["next_reset"]:
        parts.append("week: rolled over, run /usage-recalibrate")
        result["weekly_stale"] = True
        result["weekly_stale_since"] = wk["next_reset"]
    else:
        tokens_now = json.loads(
            subprocess.check_output([sys.executable, os.path.join(SCRIPTS, "tokens-since.py"), wk["window_start"]])
        )
        all_now = sum(tokens_now.values())
        tracked_now = sum(v for k, v in tokens_now.items() if TRACK_MODEL.lower() in k.lower())

        all_pct = wk["all"]["pct"] * (all_now / max(wk["all"]["tokens_at_cal"], 1))
        tm = wk["tracked_model"]
        tracked_pct = tm["pct"] * (tracked_now / max(tm["tokens_at_cal"], 1)) if tm["tokens_at_cal"] else 0

        all_pct = min(all_pct, 150)
        tracked_pct = min(tracked_pct, 150)
        resets_in = fmt_delta(wk["next_reset"], now)

        parts.append(f"week all: {all_pct:.0f}% | week {tm['name']}: {tracked_pct:.0f}% (resets {resets_in})")
        result["weekly_all_pct"] = round(all_pct, 1)
        result["weekly_tracked_model"] = tm["name"]
        result["weekly_tracked_model_pct"] = round(tracked_pct, 1)
        result["weekly_resets_at"] = wk["next_reset"]
        result["weekly_stale"] = False

    tasks = pending_tasks_count()
    if tasks is not None:
        parts.append(f"pending: {tasks}")
        result["pending_tasks"] = tasks

    line = " | ".join(parts)

    if "--json" in sys.argv:
        result["line"] = line
        print(json.dumps(result))
    else:
        print(line)


if __name__ == "__main__":
    main()
