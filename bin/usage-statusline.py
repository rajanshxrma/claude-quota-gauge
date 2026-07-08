#!/usr/bin/env python3
"""The statusLine command. Claude Code (>=2.1.80) feeds this script the
real rate_limits (five_hour, seven_day used_percentage + resets_at) on
stdin every render -- straight from Anthropic's own backend, no local
estimation, no browser scraping, no stored credentials, for those two
numbers. This script prints the statusline text and caches the numbers to
disk so the SessionStart hook and the background watcher can read the
latest known real values without needing stdin themselves.

It also surfaces one estimated number alongside the two real ones: a
per-model weekly % (default Fable) that rate_limits doesn't expose at all.
That figure is scaled from local token deltas against a manual calibration
(see usage-calibrate-fable.py) and is always labeled "(est.)" -- never
presented with the same confidence as the two verified numbers above it.
"""
import sys, os, json
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from usage_common import pending_tasks_count, fmt_delta, load_env_file, version_lt, fable_estimate  # noqa: E402

load_env_file()

SCRIPTS = os.path.expanduser("~/.claude/scripts")
CACHE_PATH = os.path.join(SCRIPTS, "usage-live.json")
MIN_VERSION = "2.1.80"


def main():
    now = datetime.now(timezone.utc)
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    rate_limits = payload.get("rate_limits") or {}
    parts = []

    # Load whatever was last cached so a transient miss below never wipes
    # out the last-known-real numbers or the fable calibration state.
    cache = {}
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH) as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    if not rate_limits:
        # Only blame the CLI version when the payload actually confirms it's
        # old -- an empty rate_limits can also mean "no API response yet this
        # session" or a free-tier account, neither of which is a version
        # problem. Never assert a cause we haven't verified.
        cli_version = payload.get("version")
        if cli_version and version_lt(cli_version, MIN_VERSION):
            parts.append(f"usage: unavailable (Claude Code {cli_version} < {MIN_VERSION})")
        else:
            parts.append("usage: unavailable")
    else:
        cache["fetched_at"] = now.isoformat()
        five_hour = rate_limits.get("five_hour") or {}
        seven_day = rate_limits.get("seven_day") or {}

        if "used_percentage" in five_hour:
            pct = five_hour["used_percentage"]
            resets_at = five_hour.get("resets_at")
            resets = fmt_delta(resets_at, now)
            parts.append(f"5h: {pct:.0f}%" + (f" (resets {resets})" if resets else ""))
            cache["five_hour_pct"] = pct
            cache["five_hour_resets_at"] = resets_at

        if "used_percentage" in seven_day:
            pct = seven_day["used_percentage"]
            resets_at = seven_day.get("resets_at")
            resets = fmt_delta(resets_at, now)
            parts.append(f"week: {pct:.0f}%" + (f" (resets {resets})" if resets else ""))
            cache["seven_day_pct"] = pct
            cache["seven_day_resets_at"] = resets_at

    fable = fable_estimate(now, cache.get("seven_day_resets_at"))
    if fable:
        cache["fable_tracked_model"] = fable["tracked_model"]
        cache["fable_stale"] = fable["stale"]
        if fable["stale"]:
            parts.append(f"{fable['tracked_model']}: stale, run /gauge-cali-fable")
            cache.pop("fable_pct", None)
            cache.pop("fable_resets_at", None)
        else:
            resets = fmt_delta(fable["resets_at"], now)
            parts.append(f"{fable['tracked_model']}: {fable['pct']:.0f}% est." + (f" (resets {resets})" if resets else ""))
            cache["fable_pct"] = fable["pct"]
            cache["fable_resets_at"] = fable["resets_at"]

    os.makedirs(SCRIPTS, exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f)

    tasks = pending_tasks_count()
    if tasks is not None:
        parts.append(f"pending: {tasks}")

    print(" | ".join(parts))


if __name__ == "__main__":
    main()
