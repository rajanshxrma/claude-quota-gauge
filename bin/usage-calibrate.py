#!/usr/bin/env python3
"""Writes a calibration snapshot after reading real percentages off
claude.ai/settings/usage. Anchors the weekly window to the account's real
weekly reset (day/hour/timezone, configurable -- NOT ccusage's Mon-Sun
calendar week, which doesn't match).

Usage: usage-calibrate.py <session_pct> <weekly_all_pct> <weekly_tracked_model_pct>
"""
import sys, os, shutil, json, subprocess
from datetime import datetime, timedelta, timezone


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

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("CLAUDE_USAGE_RESET_TZ", "America/New_York"))
except Exception:
    TZ = None

RESET_DAY = int(os.environ.get("CLAUDE_USAGE_RESET_DAY", "5"))  # Mon=0 ... Sun=6, default Sat
RESET_HOUR = int(os.environ.get("CLAUDE_USAGE_RESET_HOUR", "5"))
TRACK_MODEL = os.environ.get("CLAUDE_USAGE_TRACK_MODEL", "fable")

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


def most_recent_reset(now_local):
    # RESET_HOUR:00 local time on RESET_DAY, most recent occurrence at or before now
    days_since = (now_local.weekday() - RESET_DAY) % 7
    candidate = (now_local - timedelta(days=days_since)).replace(
        hour=RESET_HOUR, minute=0, second=0, microsecond=0
    )
    if candidate > now_local:
        candidate -= timedelta(days=7)
    return candidate


def main():
    session_pct = float(sys.argv[1])
    weekly_all_pct = float(sys.argv[2])
    weekly_tracked_model_pct = float(sys.argv[3])

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(TZ) if TZ else now_utc

    window_start_local = most_recent_reset(now_local)
    next_reset_local = window_start_local + timedelta(days=7)
    window_start_utc = window_start_local.astimezone(timezone.utc)
    next_reset_utc = next_reset_local.astimezone(timezone.utc)

    block = json.loads(subprocess.check_output(CCUSAGE_CMD + ["blocks", "--active", "--json"]))
    blocks = block.get("blocks", [])
    if not blocks:
        print("No active block from ccusage -- run this while a session is active.", file=sys.stderr)
        sys.exit(1)
    b = blocks[0]

    tokens_since = json.loads(
        subprocess.check_output(
            [sys.executable, os.path.join(SCRIPTS, "tokens-since.py"), window_start_utc.isoformat()]
        )
    )
    tracked_tokens = sum(v for k, v in tokens_since.items() if TRACK_MODEL.lower() in k.lower())
    all_tokens = sum(tokens_since.values())

    cal = {
        "calibrated_at": now_utc.isoformat(),
        "session": {
            "pct": session_pct,
            "block_start": b["startTime"],
            "block_end": b["endTime"],
            "block_cost_at_cal": b["costUSD"],
        },
        "weekly": {
            "window_start": window_start_utc.isoformat(),
            "next_reset": next_reset_utc.isoformat(),
            "all": {"pct": weekly_all_pct, "tokens_at_cal": all_tokens},
            "tracked_model": {"name": TRACK_MODEL, "pct": weekly_tracked_model_pct, "tokens_at_cal": tracked_tokens},
        },
    }
    with open(CAL_PATH, "w") as f:
        json.dump(cal, f, indent=2)
    print(f"Calibration written to {CAL_PATH}")
    print(json.dumps(cal, indent=2))


if __name__ == "__main__":
    main()
