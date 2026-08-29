#!/usr/bin/env python3
"""PreModelSwitch + PostModelSwitch hook (Claude Code >= 2.1.251): makes a
model switch quota-aware, one script wired to both events.

  PreModelSwitch (can block): annotates the switch with the real 5h/weekly %
  from the live cache, plus the tracked model's calibrated weekly estimate
  when the switch lands ON that model -- so the numbers are in front of
  Claude at the exact moment the decision to move onto a scarcer pool is
  being made, not discovered a few turns later from the bar. Optionally
  (CLAUDE_USAGE_SWITCH_BLOCK_PCT, off by default) denies a switch onto the
  tracked model once its estimate is at or past that %, since moving onto a
  pool that's effectively spent just converts the next prompt into a
  rate-limit error.

  PostModelSwitch (informational, async): when the session moves onto OR
  off the tracked model, marks it as freshly used the same way an Agent
  dispatch with model=<tracked> already does (fable-agent-posttooluse-
  hook.py), so the very next prompt forces a silent recalibration. This
  closes the one gap the Agent-dispatch trigger couldn't see: an
  *interactive* session running on the tracked model directly (`/model
  fable`, or a settings pin) never dispatches an Agent for it, so its whole
  usage rode on the blind max-age/drift backstop -- exactly the case a
  Max-plan user switching to Fable for a hard task lands in.

Never slows or breaks a switch on its own account: any unexpected payload,
missing cache, or internal error exits 0 with no output, and Claude Code
treats that as "allow, nothing to add". The only path that ever blocks is
the explicit opt-in threshold above.
"""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from usage_common import (  # noqa: E402
    fable_mark_session_used,
    fable_mark_used,
    fmt_window,
    load_env_file,
)

load_env_file()

SCRIPTS = os.path.expanduser("~/.claude/scripts")
CACHE_PATH = os.path.join(SCRIPTS, "usage-live.json")


def _load_cache():
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def tracked_model_name(cache):
    """The model with its own calibrated weekly pool -- the env var wins
    (same knob the calibrator reads), else whatever the last calibration
    recorded, else the project default."""
    name = os.environ.get("CLAUDE_USAGE_TRACK_MODEL") or cache.get("fable_tracked_model") or "fable"
    return name.strip().lower()


def is_tracked(model_id, tracked):
    """Substring match against the raw model id -- Claude Code's canonical
    names ('claude-fable-5', 'claude-fable-5[1m]', 'fable') all contain the
    bare tracked name, and the calibrator matches transcripts the same way."""
    return bool(model_id) and tracked in str(model_id).lower()


def block_threshold():
    raw = os.environ.get("CLAUDE_USAGE_SWITCH_BLOCK_PCT", "0")
    try:
        return float(raw)
    except ValueError:
        return 0.0


def describe(cache, now, to_tracked, tracked):
    parts = []
    if "five_hour_pct" in cache:
        parts.append(fmt_window("5h", cache["five_hour_pct"], cache.get("five_hour_resets_at"), now))
    if "seven_day_pct" in cache:
        parts.append(fmt_window("week", cache["seven_day_pct"], cache.get("seven_day_resets_at"), now))
    if to_tracked and "fable_pct" in cache:
        note = "estimate, stale" if cache.get("fable_stale") else None
        parts.append(fmt_window(f"{tracked} weekly", cache["fable_pct"], cache.get("fable_resets_at"), now, note=note))
    return " | ".join(parts)


def pre_switch(payload, cache, now):
    to_model = payload.get("to_model")
    from_model = payload.get("from_model")
    tracked = tracked_model_name(cache)
    to_tracked = is_tracked(to_model, tracked)
    summary = describe(cache, now, to_tracked, tracked)

    out = {"hookSpecificOutput": {"hookEventName": "PreModelSwitch"}}
    if summary:
        out["systemMessage"] = (
            f"Model switch {from_model} → {to_model}. Usage at this moment "
            f"(real rate_limits, cached by the statusline): {summary}."
        )

    threshold = block_threshold()
    if to_tracked and threshold > 0 and "fable_pct" in cache:
        pct = float(cache["fable_pct"])
        if pct >= threshold:
            out["hookSpecificOutput"]["permissionDecision"] = "deny"
            out["hookSpecificOutput"]["permissionDecisionReason"] = (
                f"{tracked} weekly pool is at ~{pct:.0f}% (block threshold "
                f"{threshold:.0f}%, CLAUDE_USAGE_SWITCH_BLOCK_PCT). Switching "
                f"onto it now would only turn the next prompt into a rate-limit "
                f"error -- stay on {from_model} or wait for the reset."
            )
    return out


def post_switch(payload, cache, now):
    tracked = tracked_model_name(cache)
    touched = is_tracked(payload.get("to_model"), tracked) or is_tracked(payload.get("from_model"), tracked)
    if not touched:
        return None
    # Onto the tracked model: this session is now spending its pool, and the
    # blind schedule would otherwise be the only thing watching. Off it: the
    # "end of a tracked-model task" moment, the same one the Agent-dispatch
    # trigger fires on -- worth a fresh read of the settings page either way.
    try:
        fable_mark_used(now, context="model-switch")
    except Exception:
        pass
    if is_tracked(payload.get("to_model"), tracked):
        try:
            fable_mark_session_used(payload.get("session_id"), now)
        except Exception:
            pass
    return None


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    event = payload.get("hook_event_name")
    now = datetime.now(timezone.utc)
    cache = _load_cache()
    try:
        if event == "PreModelSwitch":
            out = pre_switch(payload, cache, now)
        elif event == "PostModelSwitch":
            out = post_switch(payload, cache, now)
        else:
            out = None
    except Exception:
        out = None
    if out:
        print(json.dumps(out))


if __name__ == "__main__":
    main()
