"""Tests for 0.23.0: the incremental transcript scan (bin/tokens-since.py),
the redraw that never waits on it (scan_totals() and fable_estimate() in
bin/usage_common.py), and the live work progress row (the clock in seconds,
the creeping fill, the pulse, and the switch that turns them off).

Run with:  python3 -m unittest discover -s tests -v

Same isolation rule as the rest of the suite: every script runs against a
throwaway HOME with CLAUDE_USAGE_*, CLAUDE_CODE_SESSION_ID, NO_COLOR and
COLUMNS stripped from the inherited environment. Every transcript here is
invented: one usage record per line, no conversation text at all.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(REPO_ROOT, "bin")
FIXTURE = os.path.join(REPO_ROOT, "tests", "fixtures", "progress_rows_0.22.1.json")
STRIPPED = {"CLAUDE_CODE_SESSION_ID", "NO_COLOR", "CLICOLOR_FORCE", "COLUMNS"}
ANSI = re.compile("\x1b\\[[0-9;]*m")
WEEK = timedelta(days=7)


def isolated_env(home, extra=None):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLAUDE_USAGE") and k not in STRIPPED}
    env.update({"HOME": home, "USERPROFILE": home, "PYTHONUTF8": "1",
                "PATH": os.environ.get("PATH", "/usr/bin:/bin")})
    env.update(extra or {})
    return env


def usage_line(when, model="claude-fable-5", input_tokens=1_000_000):
    """One invented transcript record. claude-fable-5 input is $10/1M, so
    the default is exactly $10.00 of cost-weighted usage."""
    return json.dumps({
        "timestamp": when.isoformat().replace("+00:00", "Z"),
        "message": {"model": model, "usage": {"input_tokens": input_tokens, "output_tokens": 0}},
    }) + "\n"


class Home(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.scripts = os.path.join(self.home, ".claude", "scripts")
        self.projects = os.path.join(self.home, ".claude", "projects")
        os.makedirs(self.scripts)
        os.makedirs(self.projects)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def tearDown(self):
        self._wait_for_scanner()
        self._tmp.cleanup()

    def _wait_for_scanner(self, timeout=15):
        """A detached scanner started by a redraw holds the lock until it is
        done; wait on the lock file rather than on any process name."""
        lock = os.path.join(self.scripts, "tokens-since.lock")
        deadline = time.time() + timeout
        while os.path.exists(lock) and time.time() < deadline:
            time.sleep(0.05)

    def transcript(self, name):
        path = os.path.join(self.projects, "proj", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def append(self, path, text):
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


class IncrementalScanTest(Home):
    """bin/tokens-since.py keeps a record per file and reads only what was
    appended; each case checks the result against a scan from nothing."""

    def setUp(self):
        super().setUp()
        self.start = (self.now - timedelta(days=2)).isoformat()

    def scan(self, *args, fresh=False):
        if fresh:
            for name in ("tokens-since-scan.json", "tokens-since-totals.json"):
                try:
                    os.remove(os.path.join(self.scripts, name))
                except FileNotFoundError:
                    pass
        r = subprocess.run([sys.executable, os.path.join(BIN, "tokens-since.py"), *args, self.start],
                           capture_output=True, text=True, env=isolated_env(self.home), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def record(self):
        with open(os.path.join(self.scripts, "tokens-since-scan.json")) as f:
            return json.load(f)["starts"][self.start]["files"]

    def fable(self, totals):
        return round(totals.get("claude-fable-5", 0), 6)

    def test_an_append_is_read_from_the_last_offset(self):
        path = self.transcript("a.jsonl")
        self.append(path, usage_line(self.now - timedelta(hours=1)))
        self.assertEqual(self.fable(self.scan()), 10.0)
        first_offset = self.record()[path][3]
        self.assertEqual(first_offset, os.path.getsize(path))
        self.append(path, usage_line(self.now))
        self.assertEqual(self.fable(self.scan()), 20.0)
        self.assertEqual(self.record()[path][3], os.path.getsize(path))
        self.assertEqual(self.fable(self.scan()), 20.0, "an unchanged file counted twice")
        self.assertEqual(self.scan(fresh=True), self.scan())

    def test_a_line_still_being_written_waits_for_its_end(self):
        path = self.transcript("a.jsonl")
        self.append(path, usage_line(self.now))
        whole = usage_line(self.now)
        self.append(path, whole[:25])
        self.assertEqual(self.fable(self.scan()), 10.0)
        self.append(path, whole[25:])
        self.assertEqual(self.fable(self.scan()), 20.0)

    def test_a_file_that_shrank_is_counted_again_from_zero(self):
        path = self.transcript("a.jsonl")
        for _ in range(3):
            self.append(path, usage_line(self.now))
        self.assertEqual(self.fable(self.scan()), 30.0)
        with open(path, "w", encoding="utf-8") as f:
            f.write(usage_line(self.now, input_tokens=500_000))
        self.assertEqual(self.fable(self.scan()), 5.0)

    def test_a_replaced_file_is_counted_again_from_zero(self):
        path = self.transcript("a.jsonl")
        self.append(path, usage_line(self.now))
        self.assertEqual(self.fable(self.scan()), 10.0)
        replacement = path + ".new"
        with open(replacement, "w", encoding="utf-8") as f:
            f.write(usage_line(self.now, input_tokens=200_000) * 4)
        os.replace(replacement, path)  # a new inode, larger than before
        self.assertEqual(self.fable(self.scan()), 8.0)

    def test_new_and_vanished_files(self):
        a, b = self.transcript("a.jsonl"), self.transcript("b.jsonl")
        self.append(a, usage_line(self.now))
        self.assertEqual(self.fable(self.scan()), 10.0)
        self.append(b, usage_line(self.now) * 2)
        self.assertEqual(self.fable(self.scan()), 30.0)
        os.remove(a)
        self.assertEqual(self.fable(self.scan()), 20.0)
        self.assertNotIn(a, self.record())

    def test_entries_before_the_window_and_old_files_do_not_count(self):
        path = self.transcript("a.jsonl")
        self.append(path, usage_line(self.now - timedelta(days=3)))
        self.append(path, usage_line(self.now))
        old = self.transcript("old.jsonl")
        self.append(old, usage_line(self.now - timedelta(days=5)))
        long_ago = (self.now - timedelta(days=5)).timestamp()
        os.utime(old, (long_ago, long_ago))
        self.assertEqual(self.fable(self.scan()), 10.0)
        self.assertNotIn(old, self.record())

    def test_two_scanners_at_once_agree_with_a_scan_from_nothing(self):
        for i in range(40):
            path = self.transcript(f"s{i}.jsonl")
            self.append(path, usage_line(self.now, input_tokens=100_000) * (i % 5 + 1))
        expected = 10.0 * sum(i % 5 + 1 for i in range(40)) / 10
        env = isolated_env(self.home)
        cmd = [sys.executable, os.path.join(BIN, "tokens-since.py"), self.start]
        procs = [subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
                 for _ in range(2)]
        outs = [p.communicate(timeout=60) for p in procs]
        for p, (out, err) in zip(procs, outs):
            self.assertEqual(p.returncode, 0, err)
            self.assertAlmostEqual(json.loads(out)["claude-fable-5"], expected, places=6)
        # Whatever order they finished in, the record left behind is whole
        # and a further incremental scan neither loses nor doubles anything.
        self.assertAlmostEqual(self.scan()["claude-fable-5"], expected, places=6)
        self.assertEqual(len(self.record()), 40)
        self.assertEqual(sorted(f for f in os.listdir(self.scripts) if f.endswith(".tmp")), [])

    def test_many_rounds_of_appends_match_a_scan_from_nothing(self):
        paths = [self.transcript(f"r{i}.jsonl") for i in range(5)]
        for round_ in range(6):
            for i, path in enumerate(paths):
                if (round_ + i) % 2:
                    self.append(path, usage_line(self.now, input_tokens=1000 * (round_ + i + 1)))
                    self.append(path, '{"type":"note"}\n')
            incremental = self.scan()
            with open(os.path.join(self.scripts, "tokens-since-scan.json")) as f:
                kept = f.read()
            self.assertEqual(self.scan(fresh=True), incremental)
            with open(os.path.join(self.scripts, "tokens-since-scan.json"), "w") as f:
                f.write(kept)

    def test_background_mode_respects_a_fresh_lock_and_takes_over_a_stale_one(self):
        path = self.transcript("a.jsonl")
        self.append(path, usage_line(self.now))
        lock = os.path.join(self.scripts, "tokens-since.lock")
        with open(lock, "w") as f:
            json.dump({"pid": 1, "started_at": time.time()}, f)
        self.scan("--background")
        self.assertFalse(os.path.exists(os.path.join(self.scripts, "tokens-since-totals.json")),
                         "a second scanner ran while the first held the lock")
        stale = time.time() - 300
        os.utime(lock, (stale, stale))
        self.scan("--background")
        with open(os.path.join(self.scripts, "tokens-since-totals.json")) as f:
            totals = json.load(f)["starts"][self.start]
        self.assertAlmostEqual(totals["totals"]["claude-fable-5"], 10.0)
        self.assertFalse(os.path.exists(lock), "the scanner left its lock behind")


class RedrawNeverWaitsTest(Home):
    """The quota line reads the last finished scan and returns; a scan is
    started detached (one at a time) and never waited on."""

    SLOW_SCANNER = """import json, os, sys, time
scripts = os.path.expanduser("~/.claude/scripts")
open(os.path.join(scripts, "scanner-started"), "a").write("x")
time.sleep(3)
start = sys.argv[-1]
data = {"version": 1, "starts": {start: {"totals": {"claude-fable-5": 10.0}, "scanned_at": time.time()}}}
with open(os.path.join(scripts, "tokens-since-totals.json"), "w") as f:
    json.dump(data, f)
os.remove(os.path.join(scripts, "tokens-since.lock"))
"""

    def setUp(self):
        super().setUp()
        self.app = os.path.join(self.home, "app")
        os.makedirs(self.app)
        for name in ("usage-statusline.py", "usage_common.py"):
            shutil.copy2(os.path.join(BIN, name), self.app)
        with open(os.path.join(self.app, "tokens-since.py"), "w") as f:
            f.write(self.SLOW_SCANNER)
        self.next_reset = self.now + timedelta(days=3)
        self.window_start = (self.next_reset - WEEK).isoformat()
        with open(os.path.join(self.scripts, "usage-fable-calibration.json"), "w") as f:
            json.dump({
                "calibrated_at": (self.now - timedelta(hours=1)).isoformat(),
                "tracked_model": "fable", "model": "anchor-v2", "pct": 0,
                "window_start": self.window_start, "next_reset": self.next_reset.isoformat(),
                "tokens_at_cal": 0, "cap": 100,
                "cap_derived_at": (self.now - timedelta(hours=1)).isoformat(),
            }, f)

    def render(self, extra=None):
        payload = {"model": {"id": "fable-5", "display_name": "Fable 5"},
                   "rate_limits": {"seven_day": {"used_percentage": 30,
                                                 "resets_at": int(self.next_reset.timestamp())}}}
        t = time.monotonic()
        r = subprocess.run([sys.executable, os.path.join(self.app, "usage-statusline.py"), "--json"],
                           input=json.dumps(payload), capture_output=True, text=True,
                           env=isolated_env(self.home, extra), cwd=self.home, timeout=30)
        took = time.monotonic() - t
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        return json.loads(r.stdout)["tracked_model"], took

    def started(self):
        try:
            with open(os.path.join(self.scripts, "scanner-started")) as f:
                return len(f.read())
        except FileNotFoundError:
            return 0

    def test_the_redraw_returns_while_the_scan_runs_and_uses_it_after(self):
        tracked, took = self.render()
        self.assertLess(took, 2.0, "the redraw waited on the 3 second scan")
        self.assertTrue(tracked["counting"])
        self.assertFalse(tracked["stale"], "a scan still running is not staleness")
        self.assertIsNone(tracked["pct"])
        # A second redraw while the first scan still runs starts no other.
        tracked, took = self.render()
        self.assertLess(took, 2.0)
        self._wait_for_scanner()
        self.assertEqual(self.started(), 1)
        tracked, _ = self.render()
        self.assertFalse(tracked["counting"])
        self.assertAlmostEqual(tracked["pct"], 10.0, places=6)

    def write_totals(self, age_s, fable_cost=25.0):
        with open(os.path.join(self.scripts, "tokens-since-totals.json"), "w") as f:
            json.dump({"version": 1, "starts": {self.window_start: {
                "totals": {"claude-fable-5": fable_cost}, "scanned_at": time.time() - age_s}}}, f)

    def test_a_recent_scan_is_used_as_it_is_and_starts_nothing(self):
        self.write_totals(age_s=5)
        tracked, _ = self.render()
        self.assertAlmostEqual(tracked["pct"], 25.0, places=6)
        self.assertEqual(self.started(), 0)

    def test_an_old_scan_still_projects_and_its_age_shows_only_past_the_threshold(self):
        self.write_totals(age_s=60)
        tracked, _ = self.render()
        self.assertFalse(tracked["stale"])
        self.assertAlmostEqual(tracked["pct"], 25.0, places=6)
        self._wait_for_scanner()
        self.assertEqual(self.started(), 1, "a scan older than 20s should start one refresh")
        self.write_totals(age_s=60)
        text = self.render_text()
        self.assertIn("fable: 25%", text)
        self.assertNotIn("counted", text)
        self.write_totals(age_s=600)
        text = self.render_text()
        self.assertIn("fable: 25% (counted 10m ago)", text)
        self.assertNotIn("refreshes next msg", text)

    def render_text(self):
        payload = {"model": {"id": "fable-5", "display_name": "Fable 5"},
                   "rate_limits": {"seven_day": {"used_percentage": 30,
                                                 "resets_at": int(self.next_reset.timestamp())}}}
        # Hold the lock so the text render starts no scanner of its own.
        lock = os.path.join(self.scripts, "tokens-since.lock")
        with open(lock, "w") as f:
            f.write("{}")
        try:
            r = subprocess.run([sys.executable, os.path.join(self.app, "usage-statusline.py")],
                               input=json.dumps(payload), capture_output=True, text=True,
                               env=isolated_env(self.home), cwd=self.home, timeout=30)
        finally:
            os.remove(lock)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_a_lock_older_than_two_minutes_is_taken_over(self):
        lock = os.path.join(self.scripts, "tokens-since.lock")
        with open(lock, "w") as f:
            json.dump({"pid": 1, "started_at": time.time() - 600}, f)
        stale = time.time() - 600
        os.utime(lock, (stale, stale))
        self.render()
        self._wait_for_scanner()
        self.assertEqual(self.started(), 1)


def load_common(env):
    """A fresh import of bin/usage_common.py under `env` (its paths and
    knobs are read from the environment)."""
    code = ("import sys, json; sys.path.insert(0, %r); import usage_common as u; "
            "exec(sys.stdin.read())") % BIN
    return code


class LiveRowTest(Home):
    """The live row, drawn by the real functions in a subprocess with the
    isolated HOME, from a state file the test writes with back-dated times."""

    def setUp(self):
        super().setUp()
        self.sid = "live-1"
        self.transcript_path = os.path.join(self.projects, "-proj", f"{self.sid}.jsonl")
        os.makedirs(os.path.dirname(self.transcript_path))
        with open(self.transcript_path, "w") as f:
            f.write("{}\n")
        self.make_old(self.transcript_path)

    def make_old(self, path, ago=600):
        t = time.time() - ago
        os.utime(path, (t, t))

    def write_state(self, steps, finished_ago, started_ago, eta_in=None, note=""):
        now = time.time()
        state = {"session": self.sid, "label": "release", "note": note, "steps": steps,
                 "finished": {k: now - v for k, v in finished_ago.items()},
                 "started_at": now - started_ago, "updated_at": now - 30}
        if finished_ago:
            state["last_done_at"] = now - min(finished_ago.values())
        if eta_in is not None:
            state["eta_at"] = now + eta_in
        with open(os.path.join(self.scripts, f"work-progress-{self.sid}.json"), "w") as f:
            json.dump(state, f)

    def py(self, body, extra=None):
        env = isolated_env(self.home, dict({"COLUMNS": "200", "CLAUDE_USAGE_PROGRESS_WIDTH": "12"},
                                           **(extra or {})))
        r = subprocess.run([sys.executable, "-c", load_common(env)], input=body,
                           capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def row(self, extra=None, transcript=True):
        tp = repr(self.transcript_path) if transcript else "None"
        return self.py(
            "from datetime import datetime, timezone\n"
            f"print(u.work_progress_line({self.sid!r}, datetime.now(timezone.utc), {tp}))",
            dict({"NO_COLOR": "1"}, **(extra or {}))).rstrip("\n")

    def bar(self, row):
        return row.split(" ")[1]

    def test_the_clock_counts_seconds(self):
        self.write_state(["a", "b", "c", "d"], {"a": 1200}, started_ago=2233, eta_in=3600)
        row = self.row()
        self.assertRegex(row, r"· 37m \d\ds in ·")
        off = self.row({"CLAUDE_USAGE_PROGRESS_LIVE": "0"})
        self.assertIn("· 37m in ·", off)

    def test_the_fill_creeps_inside_the_running_step_in_a_second_texture(self):
        # Four steps over 12 cells: three cells a step. One finished after
        # 20 minutes, 10 minutes into the second: half a step at that pace.
        self.write_state(["a", "b", "c", "d"], {"a": 600}, started_ago=1800)
        bar = self.bar(self.row())
        self.assertEqual(bar.count("█"), 3)
        self.assertEqual(bar.count("▒"), 1, bar)
        self.assertIn("25%", self.row(), "the percentage counts finished steps only")

    def test_the_creep_never_reaches_the_next_steps_mark(self):
        # Far past the expected length of the running step, for several
        # shapes: the creep stops short of the next step's first cell.
        for total, done, width in ((4, 1, 12), (3, 0, 10), (10, 6, 20), (7, 3, 8), (2, 1, 60)):
            steps = [f"s{i}" for i in range(total)]
            finished = {steps[i]: 36000 - 60 * i for i in range(done)} if done else {}
            self.write_state(steps, finished, started_ago=36060, eta_in=None if done else 60)
            bar = self.bar(self.row({"CLAUDE_USAGE_PROGRESS_WIDTH": str(width)}))
            confirmed = bar.rstrip("░").rstrip("▒")
            next_mark = (done + 1) * width / total
            creep_end = len(bar.rstrip("░"))
            self.assertLess(creep_end, next_mark, (total, done, width, bar))
            self.assertLessEqual(creep_end - done * width / total, 0.9 * width / total + 1e-9,
                                 (total, done, width, bar))
            self.assertEqual(len(bar), width)
            del confirmed

    def test_no_creep_without_a_pace_or_an_estimate(self):
        self.write_state(["a", "b", "c", "d"], {}, started_ago=600)
        self.assertNotIn("▒", self.row())

    def test_the_creep_is_the_same_hue_and_never_dimmed(self):
        self.write_state(["a", "b", "c", "d"], {"a": 600}, started_ago=1800)
        row = self.py(
            "from datetime import datetime, timezone\n"
            f"print(u.work_progress_line({self.sid!r}, datetime.now(timezone.utc), None))")
        self.assertIn("\x1b[38;5;63m▒\x1b[0m", row)
        self.assertNotIn("\x1b[2m", row)

    def pulses(self, n=3, extra=None):
        out = self.py(
            "from datetime import datetime, timezone\n"
            f"for _ in range({n}):\n"
            f"    print(u.work_progress_line({self.sid!r}, datetime.now(timezone.utc), {self.transcript_path!r}))",
            dict({"NO_COLOR": "1"}, **(extra or {})))
        return [re.search(r"▸ b (\S)", line).group(1) for line in out.splitlines()]

    def test_the_pulse_rests_when_nothing_moved(self):
        self.write_state(["a", "b", "c", "d"], {"a": 600}, started_ago=1800)
        self.assertEqual(self.pulses(), ["○", "○", "○"])

    def test_the_pulse_moves_on_every_redraw_while_the_session_writes(self):
        self.write_state(["a", "b", "c", "d"], {"a": 600}, started_ago=1800)
        os.utime(self.transcript_path, None)
        frames = self.pulses(4)
        self.assertEqual(len(set(frames)), 4, frames)
        self.assertTrue(all(f in "◐◓◑◒" for f in frames))

    def test_the_pulse_sees_a_subagent_or_the_tasks_folder(self):
        self.write_state(["a", "b", "c", "d"], {"a": 600}, started_ago=1800)
        agents = os.path.join(self.transcript_path[:-len(".jsonl")], "subagents")
        os.makedirs(agents)
        agent = os.path.join(agents, "agent-x.jsonl")
        with open(agent, "w") as f:
            f.write("{}\n")
        self.make_old(agents)
        self.assertNotEqual(self.pulses(2)[0], "○")
        self.make_old(agent)
        self.assertEqual(self.pulses(2), ["○", "○"])
        tasks = os.path.join(self.home, ".claude", "tasks", self.sid)
        os.makedirs(tasks)
        with open(os.path.join(tasks, "1.json"), "w") as f:
            f.write("{}")
        self.assertNotEqual(self.pulses(2)[0], "○")

    def test_ascii_mode_has_its_own_glyphs(self):
        self.write_state(["a", "b", "c", "d"], {"a": 600}, started_ago=1800)
        os.utime(self.transcript_path, None)
        row = self.row({"CLAUDE_USAGE_PROGRESS_ASCII": "1"})
        self.assertRegex(row, r"\[###=--------\]")
        self.assertRegex(row, r"> b [|/\-\\]")

    def test_the_seconds_go_before_the_note_is_dropped(self):
        self.write_state(["a", "b", "c", "d"], {"a": 600}, started_ago=1800, eta_in=3600,
                         note="migrating the fixtures")
        wide = self.row({"COLUMNS": "200"})
        self.assertRegex(wide, r"30m \d\ds in")
        self.assertIn("migrating the fixtures", wide)
        # Narrow enough that with the seconds the note would have 7 cells
        # (under the 8 it needs) and without them 11: the seconds go first.
        without_note = len(wide) - len(" · migrating the fixtures")
        narrow = self.row({"COLUMNS": str(without_note + 14)})
        self.assertIn("30m in", narrow)
        self.assertIn("migrating", narrow)

    def test_no_pulse_or_creep_or_seconds_once_finished(self):
        self.write_state(["a", "b"], {"a": 900, "b": 60}, started_ago=1800)
        row = self.row()
        self.assertIn("done in 29m", row)
        self.assertNotIn("▒", row)
        self.assertNotRegex(row, r"\d\ds")

    def test_the_switch_off_is_the_0_22_1_row_byte_for_byte(self):
        with open(FIXTURE, encoding="utf-8") as f:
            fixture = json.load(f)
        body = (
            "import json, os\n"
            f"fx = json.load(open({FIXTURE!r}, encoding='utf-8'))\n"
            "bad = []\n"
            "for c in fx['cases']:\n"
            "    if c['appearance']: os.environ['CLAUDE_USAGE_PROGRESS_APPEARANCE'] = c['appearance']\n"
            "    else: os.environ.pop('CLAUDE_USAGE_PROGRESS_APPEARANCE', None)\n"
            "    for pulse in (None, '◐'):\n"
            "        row = u.fmt_work_progress(fx['views'][c['view']], width=c['width'], color=c['color'],\n"
            "                                  ascii_only=c['ascii'], columns=c['columns'], pulse=pulse)\n"
            "        if row != c['row']: bad.append((c, row))\n"
            "print(json.dumps({'n': len(fx['cases']), 'bad': bad[:3]}))\n")
        out = json.loads(self.py(body, {"CLAUDE_USAGE_PROGRESS_LIVE": "0",
                                        "CLAUDE_USAGE_PROGRESS_WIDTH": ""}))
        self.assertEqual(out["n"], len(fixture["cases"]))
        self.assertGreater(out["n"], 100)
        self.assertEqual(out["bad"], [])

    def test_the_switch_off_draws_the_whole_statusline_row_as_0_22_1(self):
        self.write_state(["a", "b", "c", "d"], {"a": 600}, started_ago=1800, eta_in=3600, note="n")
        os.utime(self.transcript_path, None)
        out = self.py(
            "from datetime import datetime, timezone\n"
            "now = datetime.now(timezone.utc)\n"
            f"view = u.work_progress_view(u.work_progress_load({self.sid!r}), now)\n"
            f"line = u.work_progress_line({self.sid!r}, now, {self.transcript_path!r})\n"
            "old = u.fmt_work_progress(view, live=False)\n"
            "print(line == old, '▒' in line, any(g in line for g in '◐◓◑◒○'))",
            {"CLAUDE_USAGE_PROGRESS_LIVE": "0"})
        self.assertEqual(out.split(), ["True", "False", "False"])
        self.assertFalse(os.path.exists(os.path.join(self.scripts, f".work-progress-{self.sid}.pulse")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
