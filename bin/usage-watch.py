#!/usr/bin/env python3
"""Background watcher: fires a macOS notification when the last cached
usage % (written by the statusline renderer) crosses a threshold, once per
window (dedup'd against that window's reset timestamp). This reads whatever
was last cached -- it can't refresh the numbers itself, since the real
rate_limits data only arrives while a Claude Code session's statusline is
actively rendering.
"""
import json, os, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from usage_common import load_env_file  # noqa: E402

load_env_file()

SCRIPTS = os.path.expanduser("~/.claude/scripts")
CACHE_PATH = os.path.join(SCRIPTS, "usage-live.json")
STATE_PATH = os.path.join(SCRIPTS, "usage-watch-state.json")
THRESHOLD = int(os.environ.get("CLAUDE_USAGE_ALERT_THRESHOLD", "85"))


def notify(title, message):
    if sys.platform == "darwin":
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}" sound name "Glass"']
        )
    else:
        print(f"{title}: {message}")


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


def main():
    if not os.path.exists(CACHE_PATH):
        return

    with open(CACHE_PATH) as f:
        cache = json.load(f)

    state = load_state()
    changed = False

    checks = []
    if "five_hour_pct" in cache:
        checks.append(("five_hour", "5-hour block", cache["five_hour_pct"], cache.get("five_hour_resets_at")))
    if "seven_day_pct" in cache:
        checks.append(("seven_day", "weekly", cache["seven_day_pct"], cache.get("seven_day_resets_at")))
    if "fable_pct" in cache:
        model = cache.get("fable_tracked_model", "fable")
        checks.append((f"{model}_weekly", f"{model} weekly", cache["fable_pct"], cache.get("fable_resets_at")))

    for key, label, pct, resets_at in checks:
        notified_key = f"{key}_notified_for"
        if pct >= THRESHOLD and state.get(notified_key) != resets_at:
            notify("Claude usage", f"{label} at {pct:.0f}% used")
            state[notified_key] = resets_at
            changed = True
        elif pct < THRESHOLD and state.get(notified_key) is not None:
            if state.get(notified_key) != resets_at:
                state[notified_key] = None
                changed = True

    if changed:
        save_state(state)


if __name__ == "__main__":
    main()
