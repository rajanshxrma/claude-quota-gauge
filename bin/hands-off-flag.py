#!/usr/bin/env python3
"""Hands off the Mac: a red first row in the status line while a job drives
this Mac's pointer or keyboard, so whoever glances at it knows not to touch.

  hands-off-flag.py set --reason "UI test is dragging across the app" --minutes 6
  hands-off-flag.py set ... --paused   the job is waiting for the Mac to be left alone
  hands-off-flag.py clear [--pid N]    lower it (with --pid, only that owner's flag)
  hands-off-flag.py status [--json]    the row as the status line draws it
  hands-off-flag.py run --reason ".." --minutes N -- CMD ...
                                       raise it, run CMD, lower it however CMD ends

The flag is ~/.claude/hands-off.json. The status line drops a flag whose
owner (--pid; a script passes its own, `--pid $$`) has exited, or that is
more than 30 minutes past its estimate, so a killed job never leaves it
standing. Without --pid, `set` leaves the flag ownerless and only the time
rule ends it; `run` owns it itself. From
Python, write the same JSON or import usage_common: hands_off_set(reason,
minutes) / hands_off_clear(pid=os.getpid()).
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from usage_common import (  # noqa: E402
    fmt_hands_off, hands_off_clear, hands_off_load, hands_off_set,
    work_progress_ascii_default, work_progress_no_color,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     epilog="\n\n".join(__doc__.split("\n\n")[1:]),
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    for name in ("set", "run"):
        p = sub.add_parser(name, help="raise the flag" if name == "set" else "raise it around a command")
        p.add_argument("--reason", required=True, help="what is driving the Mac, in a few words")
        p.add_argument("--minutes", type=float, required=True, help="how long, honestly")
        p.add_argument("--paused", action="store_true", help="waiting for the Mac to be left alone")
        if name == "set":
            p.add_argument("--pid", type=int, help="the owning process; it goes when that process does")
        else:
            p.add_argument("cmd", nargs=argparse.REMAINDER, help="-- then the command")
    p = sub.add_parser("clear", help="lower the flag")
    p.add_argument("--pid", type=int, help="only if this process owns it")
    p = sub.add_parser("status", help="show the flag")
    p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    command = args.command or "status"

    if command == "set":
        record = hands_off_set(args.reason, args.minutes, pid=args.pid or 0, paused=args.paused)
        print(json.dumps(record, ensure_ascii=False))
        return 0
    if command == "run":
        cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
        if not cmd:
            parser.error("run needs a command after --")
        hands_off_set(args.reason, args.minutes, pid=os.getpid(), paused=args.paused)
        try:
            return subprocess.call(cmd)
        except KeyboardInterrupt:
            return 130
        finally:
            hands_off_clear(pid=os.getpid())
    if command == "clear":
        print("hands-off flag cleared" if hands_off_clear(pid=args.pid) else "no hands-off flag was set")
        return 0
    state = hands_off_load()
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2) if state else "null")
    elif state:
        print(fmt_hands_off(state, color=not work_progress_no_color() and sys.stdout.isatty(),
                            ascii_only=work_progress_ascii_default()))
    else:
        print("no hands-off flag: the Mac is free")
    return 0


if __name__ == "__main__":
    sys.exit(main())
