#!/usr/bin/env python3
"""Work progress bar for the statusline: one bar per session, set by the
session itself (or by you) at the start of a long task and kept current as
its steps finish. statusline.py draws it as an extra last line while it's
set, and not at all otherwise.

  work-progress.py set "release 2.4" --steps "build,review,test,ship" --eta 1h30m
  work-progress.py step [NAME]        mark NAME finished (default: the current step)
  work-progress.py note "text"        replace the note (no text clears it)
  work-progress.py eta 40m | off      restate the estimate, counted from now
  work-progress.py status [--json]    everything the bar knows, for people or scripts
  work-progress.py clear              remove the bar
  work-progress.py segment            the statusline text itself

`done` is another name for `step`; `bump` finishes the current step without
naming it. Without --steps, `set --total N` makes N unnamed steps. Running
`set` again with the same label while the bar is still going re-plans it
and keeps its clock and finished steps (`--restart` starts the clock over).

Every command takes --session ID. Without it the id comes from
CLAUDE_CODE_SESSION_ID, which Claude Code sets in every shell a session
runs, so a session's own commands land on its own bar with nothing passed.

State is ~/.claude/scripts/work-progress-<session>.json, next to the rest
of this tool's state, written atomically under a lock so parallel lanes
finishing at the same moment all count. Files untouched for a week are
pruned on the next `set`. See usage_common.py (work_progress_*) for how the
bar is read, when it hides, and how the time left is worked out.
"""
import argparse
import contextlib
import glob
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from usage_common import (  # noqa: E402
    WORK_PROGRESS_DIR, fmt_span, fmt_span_approx, fmt_work_progress, load_env_file,
    work_progress_ascii_default, work_progress_left_text, work_progress_load,
    work_progress_no_color, work_progress_path, work_progress_pulse_path, work_progress_view,
)

try:
    import fcntl
except ImportError:  # Windows: writes stay atomic, only the cross-process lock is lost
    fcntl = None

LOCK_PATH = os.path.join(WORK_PROGRESS_DIR, ".work-progress.lock")
PRUNE_AFTER_DAYS = 7
_DURATION_RE = re.compile(r"^(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?:in)?)?$")


def duration(text):
    """Seconds from '90' (minutes), '90m', '1h', '1h30m', '1.5h' or
    '1h 30m'. An argparse type, so a bad value is a usage error."""
    t = str(text).strip().lower().replace(" ", "")
    if re.fullmatch(r"\d+(?:\.\d+)?", t):
        return float(t) * 60
    m = _DURATION_RE.match(t)
    if not t or not m or not (m.group(1) or m.group(2)):
        raise argparse.ArgumentTypeError(f"not a duration: {text!r} (try 45m, 1h30m or 90)")
    return float(m.group(1) or 0) * 3600 + float(m.group(2) or 0) * 60


def minutes(text):
    """--eta-min / --quiet-min: a plain number of minutes."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number of minutes: {text!r}")
    if value < 0:
        raise argparse.ArgumentTypeError("minutes can't be negative")
    return value


@contextlib.contextmanager
def locked():
    """One lock for every session's bar: writes take milliseconds, so a
    shared lock costs nothing and needs no per-session lock files."""
    os.makedirs(WORK_PROGRESS_DIR, exist_ok=True)
    if fcntl is None:
        yield
        return
    with open(LOCK_PATH, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def read_state(sid):
    """(state, problem): the normalized state, or None plus why not."""
    path = work_progress_path(sid)
    if not os.path.exists(path):
        return None, "no progress bar is set for this session"
    state = work_progress_load(sid)  # the same reader the statusline uses
    if state is None:
        return None, f"the bar's state file is unreadable ({path}); run `set` to start over"
    return state, None


def write_state(sid, state):
    """Atomic: a temp file in the same directory, then os.replace(), so a
    reader only ever sees the old file or the new one, never half of one."""
    path = work_progress_path(sid)
    record = {k: v for k, v in state.items() if k != "measured"}
    if not state["steps"]:
        # unnamed steps keep their pace baseline as a count; named steps
        # keep it as finish times (None for steps passed in as done)
        record["done_at_start"] = state["done"] - state["measured"]
    fd, tmp = tempfile.mkstemp(dir=WORK_PROGRESS_DIR, prefix=".work-progress-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def prune(now_ts):
    """Per-session files would pile up one per session forever; any bar
    untouched for PRUNE_AFTER_DAYS (long hidden by then) is removed, along
    with temp files a killed write left behind."""
    cutoff = now_ts - PRUNE_AFTER_DAYS * 86400
    paths = glob.glob(os.path.join(WORK_PROGRESS_DIR, "work-progress-*.json"))
    paths += glob.glob(os.path.join(WORK_PROGRESS_DIR, ".work-progress-*.tmp"))
    paths += glob.glob(os.path.join(WORK_PROGRESS_DIR, ".work-progress-*.pulse"))
    for path in paths:
        with contextlib.suppress(OSError):
            if os.path.getmtime(path) < cutoff:
                os.remove(path)


def recount(state):
    if state["steps"]:
        state["done"] = len(state["finished"])
        state["measured"] = sum(1 for t in state["finished"].values() if t is not None)


def match_step(steps, name):
    """Exact name, then case-insensitive, then a unique case-insensitive prefix."""
    if name in steps:
        return name
    lowered = name.lower()
    same = [s for s in steps if s.lower() == lowered]
    if same:
        return same[0]
    prefixed = [s for s in steps if s.lower().startswith(lowered)]
    return prefixed[0] if len(prefixed) == 1 else None


def apply_common(args, state, now_ts):
    """--note / --eta / --eta-min, shared by set, step, bump and note."""
    if getattr(args, "note", None) is not None:
        state["note"] = args.note
    eta = getattr(args, "eta", None)
    if eta is None and getattr(args, "eta_min", None) is not None:
        eta = args.eta_min * 60
    if eta is not None:
        state["eta_at"] = now_ts + eta
        state["eta_done"] = state["done"]  # the steps this estimate already knows about


def soft(message):
    """Nothing to act on: said on stderr, exit 0. The bar reports on the
    work, so a missing bar must never fail the command chain around it."""
    print(f"work-progress: {message}", file=sys.stderr)
    return 0


def cli_color():
    """Colour for what a command prints back: only to a real terminal (or
    with CLICOLOR_FORCE=1), never under NO_COLOR. `segment` differs: it
    always colours unless NO_COLOR, because Claude Code reads it via a pipe."""
    if work_progress_no_color():
        return False
    return sys.stdout.isatty() or os.environ.get("CLICOLOR_FORCE") == "1"


def echo(sid, now_ts):
    state, _ = read_state(sid)
    if state:
        view = work_progress_view(state, datetime.fromtimestamp(now_ts, timezone.utc))
        print(fmt_work_progress(view, color=cli_color(), ascii_only=work_progress_ascii_default()))
    return 0


def cmd_set(args, sid, now_ts, parser):
    label = (args.label if args.label is not None else args.label_pos or "").strip()
    steps = [s.strip() for s in (args.steps or "").split(",") if s.strip()]
    if len(set(steps)) != len(steps):
        parser.error("step names must be unique")
    if steps and args.total is not None and args.total != len(steps):
        parser.error(f"--total {args.total} doesn't match the {len(steps)} names in --steps")
    total = len(steps) or (args.total if args.total is not None else 1)
    if total < 1:
        parser.error("--total must be at least 1")
    old, _ = read_state(sid)
    replan = False
    if old and not args.restart and old["label"] == label:
        view = work_progress_view(old, datetime.fromtimestamp(now_ts, timezone.utc))
        replan = view["visible"] and not view["finished"]
    state = {"session": sid, "label": label, "note": "", "steps": steps, "finished": {},
             "total": total, "done": 0, "measured": 0, "started_at": now_ts,
             "updated_at": now_ts, "last_done_at": None, "finished_at": None,
             "eta_at": None, "eta_done": None, "quiet_min": args.quiet_min}
    if replan:
        for key in ("started_at", "last_done_at", "note", "eta_at", "eta_done"):
            state[key] = old[key]
        if args.quiet_min is None:
            state["quiet_min"] = old["quiet_min"]
        if steps and old["steps"]:
            state["finished"] = {n: t for n, t in old["finished"].items() if n in steps}
        elif steps:
            state["finished"] = {n: None for n in steps[:old["done"]]}
        else:
            state["done"] = min(old["done"], total)
            state["measured"] = min(old["measured"], state["done"])
    if args.done is not None:
        k = max(0, min(args.done, total))
        stamp = now_ts if replan else None  # re-plan: done as of now; fresh: passed in
        if steps:
            for name in steps[:k]:
                state["finished"].setdefault(name, stamp)
        else:
            if replan and k > state["done"]:
                state["measured"] += k - state["done"]
                state["last_done_at"] = now_ts
            state["done"] = k
            state["measured"] = min(state["measured"], k)
    recount(state)
    apply_common(args, state, now_ts)
    if state["done"] >= state["total"]:
        state["finished_at"] = now_ts
    prune(now_ts)
    write_state(sid, state)
    return echo(sid, now_ts)


def cmd_step(args, sid, now_ts, parser):
    state, problem = read_state(sid)
    if not state:
        return soft(problem)
    if state["done"] >= state["total"]:
        return soft(f"all {state['total']} steps are already finished")
    name = " ".join(getattr(args, "name", None) or []).strip()
    if state["steps"]:
        target = match_step(state["steps"], name) if name else next(
            n for n in state["steps"] if n not in state["finished"])
        if target is None:
            return soft(f"no step named {name!r}; the steps are: {', '.join(state['steps'])}")
        if target in state["finished"]:
            return soft(f"{target!r} is already finished")
        state["finished"][target] = now_ts
        recount(state)
    else:
        state["done"] += 1
        state["measured"] += 1
        if name and getattr(args, "note", None) is None:
            state["note"] = f"{name} done"
    state["last_done_at"] = state["updated_at"] = now_ts
    if state["done"] >= state["total"]:
        state["finished_at"] = now_ts
    apply_common(args, state, now_ts)
    write_state(sid, state)
    return echo(sid, now_ts)


def cmd_note(args, sid, now_ts, parser):
    state, problem = read_state(sid)
    if not state:
        return soft(problem)
    state["note"] = " ".join(args.text).strip()
    args.note = None  # the positional text is the note; apply_common only takes the eta
    apply_common(args, state, now_ts)
    state["updated_at"] = now_ts
    write_state(sid, state)
    return echo(sid, now_ts)


def cmd_eta(args, sid, now_ts, parser):
    state, problem = read_state(sid)
    if not state:
        return soft(problem)
    if args.value.strip().lower() in ("off", "none", "clear"):
        state["eta_at"] = None
    else:
        try:
            state["eta_at"] = now_ts + duration(args.value)
        except argparse.ArgumentTypeError as e:
            parser.error(str(e))
        state["eta_done"] = state["done"]
    state["updated_at"] = now_ts
    write_state(sid, state)
    return echo(sid, now_ts)


def cmd_clear(args, sid, now_ts, parser):
    with contextlib.suppress(OSError, TypeError):
        os.remove(work_progress_pulse_path(sid))  # the pulse's frame counter
    try:
        os.remove(work_progress_path(sid))
    except FileNotFoundError:
        return soft("no progress bar was set for this session")
    print("work progress cleared")
    return 0


def cmd_segment(args, sid, now_ts, parser):
    state, _ = read_state(sid)
    if not state:
        return 0
    view = work_progress_view(state, datetime.fromtimestamp(now_ts, timezone.utc))
    if view["visible"]:
        sys.stdout.write(fmt_work_progress(
            view, width=args.width, color=not work_progress_no_color(),
            ascii_only=args.ascii or work_progress_ascii_default()))
    return 0


def status_lines(view, color, ascii_only):
    lines = [fmt_work_progress(view, color=color, ascii_only=ascii_only)]

    def row(key, value):
        lines.append(f"  {key:<8} {value}")

    if view["steps"]:
        marks = {"done": "+" if ascii_only else "✓", "now": ">" if ascii_only else "▸",
                 "later": "-" if ascii_only else "·"}
        row("steps", "  ".join(
            f"{marks['done' if s['done'] else 'now' if s['name'] == view['current_step'] else 'later']}"
            f" {s['name']}" for s in view["steps"]))
    started = datetime.fromisoformat(view["started_at"]).astimezone().strftime("%H:%M")
    if view["finished"]:
        row("time", f"done in {fmt_span(view['elapsed_s'])} (started {started})")
    else:
        row("elapsed", f"{fmt_span(view['elapsed_s'])} (started {started})")
        headline, source = work_progress_left_text(view).replace(" left", ""), view["left_source"]
        if source == "blend":
            row("left", f"{headline} (blend of the estimate, ~{fmt_span_approx(view['estimate_left_s'])},"
                        f" and the pace, ~{fmt_span_approx(view['pace_left_s'])}"
                        f" at {fmt_span(view['pace_s_per_step'])} a step)")
        elif source == "pace":
            row("left", f"{headline} (from the pace, {fmt_span(view['pace_s_per_step'])} a step)")
        elif source == "estimate":
            row("left", f"{headline} (from the estimate)")
        elif source == "past_estimate":
            row("left", "past the estimate, with no finished step yet to measure a pace")
        else:
            row("left", "no estimate yet: set one with `eta`, or finish a step to measure the pace")
        quiet_after = view["quiet_after_s"]
        rule = f"marked quiet after {fmt_span(quiet_after)}" if quiet_after else "quiet mark off"
        row("quiet", f"{fmt_span(view['quiet_s'])} since the last update ({rule})")
    if view["note"]:
        row("note", view["note"])
    reason = view["hidden_reason"]
    shown = ("yes" if not reason else f"no, untouched for {fmt_span(view['quiet_s'])}"
             if reason == "stale" else "no, finished a while ago")
    row("shown", shown)
    return lines


def cmd_status(args, sid, now_ts, parser):
    path = work_progress_path(sid)
    state, problem = read_state(sid)
    if getattr(args, "json", False):
        out = {"session": sid, "exists": os.path.exists(path), "readable": state is not None}
        if state:
            view = work_progress_view(state, datetime.fromtimestamp(now_ts, timezone.utc))
            out.update(view)
            out["segment"] = fmt_work_progress(view, color=False, ascii_only=False, columns=0)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    if not state:
        print(problem)
        return 0
    view = work_progress_view(state, datetime.fromtimestamp(now_ts, timezone.utc))
    print("\n".join(status_lines(view, cli_color(), work_progress_ascii_default())))
    return 0


COMMANDS = {"set": cmd_set, "step": cmd_step, "done": cmd_step, "bump": cmd_step,
            "note": cmd_note, "eta": cmd_eta, "clear": cmd_clear,
            "status": cmd_status, "segment": cmd_segment}
WRITES = {"set", "step", "done", "bump", "note", "eta", "clear"}


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--session", "--session-id", dest="session", metavar="ID",
                        help="the session whose bar this is (default: $CLAUDE_CODE_SESSION_ID)")
    timing = argparse.ArgumentParser(add_help=False)
    timing.add_argument("--note", help="replace the note shown at the end of the bar")
    eta = timing.add_mutually_exclusive_group()
    eta.add_argument("--eta", type=duration, metavar="DURATION",
                     help="estimated time left from now: 45m, 1h30m, or a number of minutes")
    eta.add_argument("--eta-min", type=minutes, metavar="MIN", help="the same, in minutes")

    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0], epilog="\n\n".join(__doc__.split("\n\n")[1:3]),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session", "--session-id", dest="session_top", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("set", parents=[common, timing], help="start a bar, or re-plan the current one")
    p.add_argument("label_pos", nargs="?", metavar="LABEL", help="what the work is, e.g. \"release 2.4\"")
    p.add_argument("--label", help="the same as LABEL")
    p.add_argument("--steps", help='comma-separated step names, e.g. "build,review,test,ship"')
    p.add_argument("--total", type=int, help="number of unnamed steps (default 1)")
    p.add_argument("--done", type=int, help="steps already finished")
    p.add_argument("--quiet-min", type=minutes, metavar="MIN",
                   help="minutes without an update before the bar is marked quiet (0 = never)")
    p.add_argument("--restart", action="store_true", help="start the clock over even for the same label")

    p = sub.add_parser("step", aliases=["done"], parents=[common, timing],
                       help="mark a step finished (default: the current one)")
    p.add_argument("name", nargs="*", help="the step to mark finished")
    sub.add_parser("bump", parents=[common, timing], help="mark the current step finished")

    p = sub.add_parser("note", parents=[common, timing], help="replace the note (no text clears it)")
    p.add_argument("text", nargs="*")
    p = sub.add_parser("eta", parents=[common], help="restate the estimate from now, or `off`")
    p.add_argument("value", metavar="DURATION|off")
    sub.add_parser("clear", parents=[common], help="remove the bar")
    p = sub.add_parser("status", parents=[common], help="everything the bar knows")
    p.add_argument("--json", action="store_true", help="as JSON, for scripts")
    p = sub.add_parser("segment", parents=[common], help="the statusline text")
    p.add_argument("--width", type=int, metavar="CELLS", help="bar length in cells")
    p.add_argument("--ascii", action="store_true", help="plain ASCII glyphs")
    return parser


def main(argv=None):
    load_env_file()
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "status"
    sid = (getattr(args, "session", None) or args.session_top
           or os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    if not work_progress_path(sid):
        if command == "segment":
            return 0
        parser.error("no session id: pass --session ID, or run this inside a Claude Code "
                     "session (it sets CLAUDE_CODE_SESSION_ID for every shell)")
    now_ts = datetime.now(timezone.utc).timestamp()
    if command in WRITES:
        with locked():
            return COMMANDS[command](args, sid, now_ts, parser)
    return COMMANDS[command](args, sid, now_ts, parser)


if __name__ == "__main__":
    sys.exit(main())
