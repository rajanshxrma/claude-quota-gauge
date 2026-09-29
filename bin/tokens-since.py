#!/usr/bin/env python3
"""Sum Claude Code usage per model since a given ISO8601 UTC timestamp, weighted
by dollar cost rather than raw token count. Reads raw local session transcripts
directly -- no ccusage dependency, no calendar-week bucketing.

Why cost-weighted: Anthropic's weekly usage pool is metered by compute cost,
not token count. Output tokens run 5x the price of input on every current
model, cache reads are ~10% of input price, cache writes are ~125%, and Fable
5 costs 2x Opus per token. Summing raw tokens 1:1 drifts from the real
percentage whenever the mix of token types or models shifts between
calibrations -- weighting by list price is the closest available proxy for
the underlying compute-cost metric.

Incremental (0.23.0). A week of transcripts is more than a gigabyte, and
reading all of it on every call took 5 to 12 seconds. The scan now keeps a
record per transcript file -- its size, modification time, inode, the byte
offset already counted and the totals counted up to there -- and reads only
what was appended since. A file that shrank, changed inode, or was rewritten
in place (same size, new modification time) is counted again from zero; a
file that vanished drops out with its totals. A file last modified a day
before the window began cannot hold an entry inside it and is not opened.
Only complete lines are counted: a line still being written is left for the
next scan. Several scanners may run at once (every open session redraws its
own status line): each builds its result from the record it read and writes
it by temporary file and rename, so the last one to finish wins with a
record that is whole and consistent, and nothing is ever counted twice.

Two files, both in ~/.claude/scripts:
  tokens-since-scan.json    the per-file record (read by the scanner only)
  tokens-since-totals.json  the finished totals per window start and the
                            time each scan finished (what a redraw reads)

Usage:
  tokens-since.py <iso_start>               scan, print the totals as JSON
  tokens-since.py --background <iso_start>  scan once under the scan lock,
                                            quietly, at low priority (what a
                                            redraw starts, detached)
"""
import sys, os, json, time

# $ per 1M tokens: (input, output). Cache write is 1.25x input (5m TTL,
# the default and only TTL these transcripts use); cache read is 0.1x input.
PRICING = {
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-opus-4-5": (5.00, 25.00),
    "claude-opus-4-1": (5.00, 25.00),
    "claude-opus-4-0": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-mythos-5": (10.00, 50.00),
}
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.1
FALLBACK_PRICING = PRICING["claude-sonnet-5"]  # unknown/future model IDs

SCAN_VERSION = 1
MAX_STARTS = 2           # window starts kept in the record (this week, last)
OLD_FILE_SLACK_S = 86400  # a file untouched this long before the window can't hold it
LOCK_STALE_S = 120       # a scan lock older than this may be taken over


def scripts_dir():
    return os.path.expanduser("~/.claude/scripts")


def projects_dir():
    return os.path.expanduser("~/.claude/projects")


def state_path():
    return os.path.join(scripts_dir(), "tokens-since-scan.json")


def totals_path():
    return os.path.join(scripts_dir(), "tokens-since-totals.json")


def lock_path():
    return os.path.join(scripts_dir(), "tokens-since.lock")


def cost(model, usage):
    price_in, price_out = PRICING.get(model, FALLBACK_PRICING)
    weighted_input = (
        usage.get("input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0) * CACHE_WRITE_MULT
        + usage.get("cache_read_input_tokens", 0) * CACHE_READ_MULT
    )
    return (weighted_input * price_in + usage.get("output_tokens", 0) * price_out) / 1_000_000


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json_atomic(path, data):
    """Temporary file in the same folder, then rename: a reader sees the old
    file or the new one, never half of either. The temporary name carries
    the pid so two writers never share one."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _start_epoch(start):
    from datetime import datetime, timezone
    try:
        parsed = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except Exception:
        return None


def _transcripts(root):
    """Every *.jsonl under the projects folder, with its stat result."""
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.name.endswith(".jsonl") and entry.is_file(follow_symlinks=False):
                            yield entry.path, entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
        except OSError:
            continue


def _count_from(path, offset, start, totals):
    """Adds the cost of every complete line past `offset` whose timestamp is
    at or after `start` into `totals`; returns the new offset (the end of the
    last complete line). Same filter as ever: only lines carrying a usage
    field are parsed, and only their usage and model are read."""
    pos = offset
    with open(path, "rb") as f:
        f.seek(offset)
        for line in f:
            if not line.endswith(b"\n"):
                break  # still being written; the next scan picks it up whole
            pos += len(line)
            if b'"usage"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            ts = d.get("timestamp")
            if not ts or not isinstance(ts, str) or ts < start:
                continue
            msg = d.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            model = msg.get("model")
            if not usage or not model or not isinstance(usage, dict):
                continue
            try:
                totals[model] = totals.get(model, 0) + cost(model, usage)
            except Exception:
                continue
    return pos


def scan(start):
    """One incremental scan for window `start`. Returns the totals and
    writes both the per-file record and the finished totals."""
    state = _read_json(state_path())
    if state.get("version") != SCAN_VERSION or not isinstance(state.get("starts"), dict):
        state = {"version": SCAN_VERSION, "starts": {}}
    entry = state["starts"].get(start)
    known = entry.get("files", {}) if isinstance(entry, dict) else {}
    start_ts = _start_epoch(start)
    skip_before = start_ts - OLD_FILE_SLACK_S if start_ts is not None else None

    files = {}
    for path, st in _transcripts(projects_dir()):
        if skip_before is not None and st.st_mtime < skip_before:
            continue
        rec = known.get(path)
        offset, file_totals = 0, {}
        if isinstance(rec, list) and len(rec) == 5:
            size, mtime_ns, ino, off, tot = rec
            same_file = ino == st.st_ino and st.st_size >= off
            rewritten = st.st_size == size and st.st_mtime_ns != mtime_ns
            if same_file and not rewritten and isinstance(tot, dict):
                offset, file_totals = off, dict(tot)
            if same_file and st.st_size == size and st.st_mtime_ns == mtime_ns and isinstance(tot, dict):
                files[path] = rec  # untouched since the last scan
                continue
        try:
            offset = _count_from(path, offset, start, file_totals)
        except OSError:
            continue  # vanished or unreadable mid-scan: drops out
        files[path] = [st.st_size, st.st_mtime_ns, st.st_ino, offset, file_totals]

    totals = {}
    for rec in files.values():
        for model, value in rec[4].items():
            totals[model] = totals.get(model, 0) + value

    finished = time.time()
    state["starts"][start] = {"files": files, "scanned_at": finished}
    for old in sorted(state["starts"], key=lambda k: state["starts"][k].get("scanned_at", 0))[:-MAX_STARTS]:
        del state["starts"][old]
    # Both writes are best-effort: a folder that can't be written still
    # gets its totals printed, as before the record existed.
    try:
        _write_json_atomic(state_path(), state)
        summary = _read_json(totals_path())
        starts = summary.get("starts") if isinstance(summary.get("starts"), dict) else {}
        starts[start] = {"totals": totals, "scanned_at": finished}
        for old in sorted(starts, key=lambda k: starts[k].get("scanned_at", 0))[:-MAX_STARTS]:
            del starts[old]
        _write_json_atomic(totals_path(), {"version": SCAN_VERSION, "starts": starts})
    except Exception:
        pass
    return totals


def _lock_owner():
    return _read_json(lock_path())


def take_lock(now=None):
    """True when this process now holds the scan lock. A lock left behind by
    a scanner that died (older than LOCK_STALE_S) is taken over."""
    now = time.time() if now is None else now
    body = json.dumps({"pid": os.getpid(), "started_at": now})
    for _ in range(2):
        try:
            fd = os.open(lock_path(), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                age = now - os.stat(lock_path()).st_mtime
            except OSError:
                continue  # released between the two calls: try again
            if age < LOCK_STALE_S:
                return False
            try:
                os.unlink(lock_path())
            except OSError:
                pass
            continue
        except OSError:
            return False
        with os.fdopen(fd, "w") as f:
            f.write(body)
        return True
    return False


def claim_lock():
    """For a scanner started with the lock already taken on its behalf:
    rewrite it with this process's own pid and start time."""
    try:
        _write_json_atomic(lock_path(), {"pid": os.getpid(), "started_at": time.time()})
    except OSError:
        pass


def release_lock():
    if _lock_owner().get("pid") == os.getpid():
        try:
            os.unlink(lock_path())
        except OSError:
            pass


def main():
    args = sys.argv[1:]
    if args and args[0] == "--background":
        held = len(args) > 2 and args[1] == "--lock-held"
        start = args[-1]
        if held:
            claim_lock()
        elif not take_lock():
            return
        try:
            try:
                os.nice(10)  # never compete with the work the bar is showing
            except (AttributeError, OSError):
                pass
            scan(start)
        except Exception:
            pass
        finally:
            release_lock()
        return
    print(json.dumps(scan(args[0])))


if __name__ == "__main__":
    main()
