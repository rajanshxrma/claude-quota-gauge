#!/usr/bin/env python3
"""Writes one entry to the session-title disambiguation cache
(title-disambig-cache.json), read by title_disambiguation() in
usage_common.py and rendered by statusline.py in place of a colliding raw
ai-title. The only writer of this cache -- called exclusively from
title-collision-prompt-hook.py's injected instruction, after a live Claude
Code turn has already dispatched an Agent with model="fable" to produce the
label (this script does no LLM call itself, just validates and persists
what was generated).

Usage: title-disambig-write.py <session_id> <source_title> <summary>
"""
import sys, os, json
from datetime import datetime, timezone

CACHE_PATH = os.path.expanduser("~/.claude/scripts/title-disambig-cache.json")
MAX_SUMMARY_CHARS = 40  # leaves headroom in title_chip()'s own budget for
                         # the "▸ " change marker and truncation ellipsis


def main():
    if len(sys.argv) != 4:
        print("usage: title-disambig-write.py <session_id> <source_title> <summary>", file=sys.stderr)
        sys.exit(1)
    session_id, source_title, summary = sys.argv[1], sys.argv[2], sys.argv[3]

    summary = " ".join(summary.strip().lower().split())  # collapse whitespace, normalize case
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[:MAX_SUMMARY_CHARS].rstrip()
    if not summary:
        print("empty summary, not writing", file=sys.stderr)
        sys.exit(1)

    try:
        with open(CACHE_PATH) as f:
            cache = json.load(f)
    except Exception:
        cache = {}

    cache[session_id] = {
        "summary": summary,
        "source_title": source_title,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f)


if __name__ == "__main__":
    main()
