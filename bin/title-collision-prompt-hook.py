#!/usr/bin/env python3
"""UserPromptSubmit hook: catches a session's title colliding with another
currently-open session's title (Claude Code's own ai-title generator will
happily give two unrelated sessions the identical title -- e.g. several
`/afk` sessions all landing on "AFK pre-flight check" -- which defeats the
whole point of the session-title chip, telling concurrent sessions apart at
a glance).

Fires once per genuinely new collision (see title_disambiguation()'s
source_title match in usage_common.py -- an existing fresh cache entry for
the CURRENT title is treated as "nothing to do", so this doesn't re-inject
every single prompt while the same collision persists), and asks the
already-loaded, free parent Claude to do the actual work: write one factual
sentence about what this session is really doing, then spend ONE Agent
dispatch with model="fable" (the only way to bill against the tracked,
subscription Fable pool rather than pay-per-token API credits -- see
api-credit-watch.py's cost-model docstring) to compress that into a short
label, then persist it via title-disambig-write.py. statusline.py picks the
cached label up on its next render -- this hook never touches the render
path itself, so a slow/failed Fable dispatch can never delay or block the
bar.
"""
import sys, os, json
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from usage_common import session_title, colliding_sessions, title_disambiguation  # noqa: E402


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return

    session_id = payload.get("session_id")
    transcript_path = payload.get("transcript_path")
    if not session_id or not transcript_path:
        return

    title = session_title(transcript_path, session_id)
    if not title:
        return

    now = datetime.now(timezone.utc)
    others = colliding_sessions(session_id, title, now)
    if not others:
        return

    # A fresh cache entry already matching this exact title means either a
    # prior firing already handled this collision, or another of the
    # colliding sessions already labeled itself -- either way, nothing new
    # for THIS session to do until its own title changes again.
    if title_disambiguation(session_id, title):
        return

    context = (
        f"This session's current title (\"{title}\") matches {len(others)} other "
        f"currently open Claude Code session(s), so the statusline chip can't tell "
        f"them apart. Silently (don't mention this in chat unless asked): "
        f"(1) write one factual sentence describing what THIS session is actually "
        f"working on right now; (2) dispatch an Agent with model=\"fable\" and this "
        f"exact prompt, substituting your sentence for <SUMMARY>:\n\n"
        f"  Session summary: <SUMMARY>. Write a distinctive terminal statusline "
        f"label for this session: 4-8 words, lowercase, no punctuation, no quotes, "
        f"naming the concrete work (not generic phrases like \"coding session\" or "
        f"\"assistant task\"). Reply with only the label.\n\n"
        f"(3) run: python3 {os.path.join(os.path.dirname(os.path.abspath(__file__)), 'title-disambig-write.py')} "
        f"{session_id} \"{title}\" \"<Fable's label>\""
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context
        }
    }))


if __name__ == "__main__":
    main()
