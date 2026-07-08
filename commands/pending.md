---
description: Append a new parked item to PENDING.md, following the newest-on-top convention
---

Do this now:

1. Find the target file: honor `$CLAUDE_USAGE_PENDING_FILE` if set (expand `~`); otherwise check `./PENDING.md`, then `~/.claude/PENDING.md`, in that order. If neither exists, create `./PENDING.md` in the current project with the header block from `examples/PENDING.md` (the `# PENDING` title plus the explanatory paragraph).
2. Take the user's argument (`$ARGUMENTS`) as the gist of the item. If it's empty, ask what to park before writing anything.
3. Insert a new `## ` heading for it directly under the file's `# PENDING` header — above every existing `## ` entry, never at the bottom — so the newest item is always on top. Write the heading as a short, specific title (not a raw copy of the argument if it's a full sentence), then a short paragraph underneath with enough detail that a cold session — yours next week, or a different model entirely — could pick it up without re-deriving context.
4. Don't touch, reorder, or reword any existing headings or their bodies.
5. Confirm back to the user with the exact heading you added and the file path it went into.
