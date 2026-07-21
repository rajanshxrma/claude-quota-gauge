#!/usr/bin/env python3
"""The statusLine command. Claude Code (>=2.1.80) feeds this script the
real rate_limits (five_hour, seven_day used_percentage + resets_at) on
stdin every render -- straight from Anthropic's own backend, no local
estimation, no browser scraping, no stored credentials, for those two
numbers. This script prints the statusline text and caches the numbers to
disk so the SessionStart hook and the background watcher can read the
latest known real values without needing stdin themselves.

It also surfaces one more number alongside the two real ones: a per-model
weekly % (default Fable) that rate_limits doesn't expose at all. That
figure is projected from cost-weighted local token usage against a weekly
$ cap derived at the last real calibration (see usage-calibrate-fable.py
and fable_estimate() in usage_common.py) -- shown without a separate
confidence label since it tracks closely (rajan's own tolerance: within
~1%), but it still reports itself as stale rather than a silently wrong
number whenever no real cap has been established, the cap needs
re-verifying, or the local projection blows past a sane ceiling.

Leads with the current session's own active model + reasoning effort
(e.g. "opusplan→Sonnet 5 (high)"), read live from the same stdin payload
(model.id/display_name, effort.level, fast_mode -- see fmt_model() in
usage_common.py). Since Claude Code invokes this command separately per
open session, each terminal's bar reflects only its own session -- no
extra bookkeeping needed to keep multiple concurrent sessions straight.

This session's own resume command (`claude --resume <full-uuid>`) used to
trail this line, right-aligned -- but the full command reads much longer
than the bare id it replaced, and cluttered this already-dense line. It's
now appended instead to the second (workload-gauge) line by statusline.py,
which has slack to spare -- see that wrapper's docstring.

Pass --json (e.g. `echo '{...}' | usage-statusline.py --json`) to get the
same data points as machine-readable JSON on stdout instead of the rendered
bar text, so other scripts/tools can consume the live quota numbers without
scraping the terminal string. Implemented as a `data` dict built alongside
`parts` inside main(), one entry per bar segment -- see the comment where
`data` is first assigned.
"""
import sys, os, json
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from usage_common import pending_tasks_count, fmt_window, load_env_file, version_lt, fable_estimate, fmt_model, fable_stale_elapsed, _cap_max_age  # noqa: E402

load_env_file()

SCRIPTS = os.path.expanduser("~/.claude/scripts")
CACHE_PATH = os.path.join(SCRIPTS, "usage-live.json")
MIN_VERSION = "2.1.80"


def main():
    json_output = "--json" in sys.argv[1:]
    now = datetime.now(timezone.utc)
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    rate_limits = payload.get("rate_limits") or {}
    parts = []
    # Mirrors `parts` one data-point at a time, for --json. Every write here
    # sits right next to the `parts.append()` it mirrors so the two can never
    # drift apart silently -- see the module docstring for which parts of
    # this are considered finished vs. still open (PAIRING_SESSION_NOTES.md).
    data = {}

    model_part = fmt_model(payload)
    if model_part:
        parts.append(model_part)
    data["model"] = model_part

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
        # An empty rate_limits usually just means no API response has landed
        # yet this render (e.g. the first statusline draw of a new session,
        # before Claude Code's first turn) -- not a real outage. Fall back to
        # the last cached real numbers instead of a bare "unavailable" so the
        # bar still shows something useful; only report "unavailable" outright
        # when there's no prior cache to fall back on.
        #
        # Tracked separately from `parts`: since v0.4.0 the model segment is
        # always prepended to `parts` before this branch runs, so `parts` is
        # never actually empty here -- checking it directly meant this whole
        # fallback (including the "upgrade Claude Code" message) could never
        # fire, and a fresh install with no cache yet just showed a blank gap
        # with zero explanation. `usage_added` tracks only the segments this
        # branch itself appends.
        usage_added = False
        data["five_hour"] = None
        data["seven_day"] = None
        if "five_hour_pct" in cache:
            parts.append(fmt_window("5h", cache["five_hour_pct"], cache.get("five_hour_resets_at"), now, cached=True))
            data["five_hour"] = {"pct": cache["five_hour_pct"], "resets_at": cache.get("five_hour_resets_at"), "cached": True}
            usage_added = True
        if "seven_day_pct" in cache:
            parts.append(fmt_window("week", cache["seven_day_pct"], cache.get("seven_day_resets_at"), now, cached=True))
            data["seven_day"] = {"pct": cache["seven_day_pct"], "resets_at": cache.get("seven_day_resets_at"), "cached": True}
            usage_added = True

        if not usage_added:
            # Only blame the CLI version when the payload actually confirms
            # it's old -- an empty rate_limits with no cache can also mean a
            # free-tier account, neither of which is a version problem. Never
            # assert a cause we haven't verified.
            cli_version = payload.get("version")
            if cli_version and version_lt(cli_version, MIN_VERSION):
                parts.append(f"usage: unavailable (Claude Code {cli_version} < {MIN_VERSION})")
                data["unavailable"] = {
                    "reason": "outdated_cli",
                    "cli_version": cli_version,
                    "min_version": MIN_VERSION,
                }
            else:
                parts.append("usage: unavailable")
                data["unavailable"] = {
                    "reason": "no_data",
                    "cli_version": None,
                    "min_version": None,
                }
    else:
        cache["fetched_at"] = now.isoformat()
        five_hour = rate_limits.get("five_hour") or {}
        seven_day = rate_limits.get("seven_day") or {}
        data["five_hour"] = None
        data["seven_day"] = None

        if "used_percentage" in five_hour:
            pct = five_hour["used_percentage"]
            resets_at = five_hour.get("resets_at")
            parts.append(fmt_window("5h", pct, resets_at, now))
            cache["five_hour_pct"] = pct
            cache["five_hour_resets_at"] = resets_at
            data["five_hour"] = {"pct": pct, "resets_at": resets_at, "cached": False}

        if "used_percentage" in seven_day:
            pct = seven_day["used_percentage"]
            resets_at = seven_day.get("resets_at")
            parts.append(fmt_window("week", pct, resets_at, now))
            cache["seven_day_pct"] = pct
            cache["seven_day_resets_at"] = resets_at
            data["seven_day"] = {"pct": pct, "resets_at": resets_at, "cached": False}

    fable = fable_estimate(now, cache.get("seven_day_resets_at"), cache.get("seven_day_pct"))
    if fable:
        cache["fable_tracked_model"] = fable["tracked_model"]
        cache["fable_stale"] = fable["stale"]
        elapsed = None
        past_grace = None
        if fable["stale"]:
            # Staleness is Claude's problem first, not the user's: the
            # SessionStart and UserPromptSubmit hooks both tell Claude to
            # recalibrate automatically the moment they see this flag, so
            # the alarming "stale, run /gauge-calibrate" bar text (from when
            # recalibration was a manual chore) would just be noise for a
            # window that typically lasts one prompt. Show the last known %
            # with the same "(refreshing…)" marker the 5h/weekly numbers use
            # while awaiting fresh data -- honest that it's being updated,
            # calm about it. The cached % is deliberately kept (not popped):
            # the hooks key off fable_stale, not the % fields, and the
            # watcher's threshold checks are better off with a slightly-old
            # number than none. The explicit command text remains only as
            # the fallback when there's no cached % to show at all.
            #
            # "refreshes next msg" rather than the generic "refreshing…":
            # the refresh trigger is the user's own next message (that's
            # when the hook nudge fires and the recalibration runs), so
            # naming the trigger tells them they can just keep working --
            # nothing here is worth sitting and waiting for.
            #
            # But that hand-off is best-effort, not guaranteed: the nudge is
            # background context a busy session can reasonably deprioritize,
            # or that never fires at all if no prompt comes in. A real
            # episode (2026-07-11 to -13) sat stale ~31h across 4 sessions
            # that each got the nudge and didn't act on it, with the bar
            # calmly claiming a same-message fix the whole time. Past one
            # grace window (one _cap_max_age -- the auto-heal's fair shot),
            # the calm framing is no longer honest, so switch to an explicit
            # elapsed-time call to action instead of repeating a promise
            # that's already been broken once this episode.
            elapsed = fable_stale_elapsed(cache, now)
            past_grace = elapsed > _cap_max_age()
            if past_grace:
                hours = elapsed.total_seconds() / 3600
                note = f"stale {hours:.0f}h — /gauge-calibrate"
                if "fable_pct" in cache:
                    parts.append(fmt_window(fable["tracked_model"], cache["fable_pct"], cache.get("fable_resets_at"), now, note=note))
                else:
                    parts.append(f"{fable['tracked_model']}: {note}")
            elif "fable_pct" in cache:
                parts.append(fmt_window(fable["tracked_model"], cache["fable_pct"], cache.get("fable_resets_at"), now, note="refreshes next msg!"))
            else:
                parts.append(f"{fable['tracked_model']}: stale, run /gauge-calibrate")
        else:
            parts.append(fmt_window(fable["tracked_model"], fable["pct"], fable["resets_at"], now))
            cache["fable_pct"] = fable["pct"]
            cache["fable_resets_at"] = fable["resets_at"]
            # Resolved -- clear any stale-episode clock so a future episode
            # (even against this same calibration, e.g. immediate re-drift)
            # starts its grace window fresh rather than inheriting elapsed
            # time from an episode that's already over.
            cache.pop("fable_stale_since", None)
            cache.pop("fable_stale_identity", None)

        # The bar text renders four distinct stale sub-states (never-
        # calibrated is handled separately below via `fable` being falsy;
        # stale-within-grace showing "refreshes next msg!"; stale-past-grace
        # showing an elapsed-hours call to action; either of the latter two
        # with no cached % to show at all). `stale_past_grace` +
        # `stale_elapsed_hours` alongside `pct` fully reconstruct which of
        # those the bar would have shown, without a script having to re-derive
        # the grace-window math itself: not stale -> both null; stale and
        # `stale_past_grace` false -> "refreshes next msg!" territory; stale
        # and `stale_past_grace` true -> the elapsed-hours call to action.
        # `pct` being null in either stale case is the "no cached % at all"
        # sub-variant.
        data["tracked_model"] = {
            "name": fable["tracked_model"],
            "stale": fable["stale"],
            "pct": fable["pct"] if not fable["stale"] else cache.get("fable_pct"),
            "resets_at": fable["resets_at"] if not fable["stale"] else cache.get("fable_resets_at"),
            "stale_elapsed_hours": round(elapsed.total_seconds() / 3600, 2) if fable["stale"] else None,
            "stale_past_grace": past_grace if fable["stale"] else None,
        }
    else:
        data["tracked_model"] = None

    os.makedirs(SCRIPTS, exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f)

    tasks = pending_tasks_count()
    if tasks is not None:
        parts.append(f"pending: {tasks}")
    data["pending_tasks"] = tasks

    if json_output:
        data["generated_at"] = now.isoformat()
        print(json.dumps(data))
    else:
        print(" | ".join(parts))


if __name__ == "__main__":
    main()
