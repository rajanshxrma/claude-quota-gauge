"""Tests for the work progress bar (0.22.0): bin/work-progress.py, the
work_progress_* helpers in bin/usage_common.py, and the extra statusline row.

Run with:  python3 -m unittest discover -s tests -v

Same isolation rule as the rest of the suite: every script runs as a
subprocess against a throwaway HOME, with CLAUDE_USAGE_*, CLAUDE_CODE_SESSION_ID,
NO_COLOR, CLICOLOR_FORCE and COLUMNS stripped from the inherited environment,
so nothing here reads or writes a real bar and the session running the tests
can't become the owner of one. Time-dependent cases write a state file with
back-dated timestamps directly, the way test_ultracode.py back-dates a
marker, instead of waiting.
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

try:
    import fcntl
except ImportError:
    fcntl = None

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(REPO_ROOT, "bin")
PROGRESS = os.path.join(BIN, "work-progress.py")
STRIPPED = {"CLAUDE_CODE_SESSION_ID", "NO_COLOR", "CLICOLOR_FORCE", "COLUMNS"}
BAR_GLYPHS = re.compile("[█▏▎▍▌▋▊▉░]")


def progress_env(home, extra=None):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLAUDE_USAGE") and k not in STRIPPED}
    env.update({"HOME": home, "USERPROFILE": home, "PYTHONUTF8": "1", "COLUMNS": "200",
                "PATH": os.environ.get("PATH", "/usr/bin:/bin")})
    env.update(extra or {})
    return env


class ProgressHome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.scripts = os.path.join(self.home, ".claude", "scripts")
        os.makedirs(self.scripts)
        self.now = time.time()

    def tearDown(self):
        self._tmp.cleanup()

    def wp(self, *args, session="s1", env=None, script=PROGRESS):
        argv = list(args) + (["--session", session] if session else [])
        return subprocess.run([sys.executable, script] + argv, capture_output=True, text=True,
                              encoding="utf-8", env=progress_env(self.home, env), cwd=self.home,
                              timeout=30)

    def segment(self, *extra, session="s1", env=None):
        r = self.wp("segment", *extra, session=session, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def plain(self, *extra, session="s1", env=None):
        return self.segment(*extra, session=session, env=dict(env or {}, NO_COLOR="1"))

    def status(self, session="s1", env=None):
        r = self.wp("status", "--json", session=session, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def state_path(self, session="s1"):
        return os.path.join(self.scripts, f"work-progress-{session}.json")

    def write_state(self, session="s1", ago_started=3600, ago_updated=60, **fields):
        """A state file as work-progress.py writes it, times given as
        seconds before now (or absolute epochs through **fields)."""
        state = {"session": session, "label": "release 2.4",
                 "started_at": self.now - ago_started, "updated_at": self.now - ago_updated}
        state.update(fields)
        with open(self.state_path(session), "w", encoding="utf-8") as f:
            json.dump(state, f)
        return state

    def four_steps(self, finished, **fields):
        """Steps build/review/test/ship, `finished` = {name: seconds ago}."""
        done = {name: self.now - ago for name, ago in finished.items()}
        last = max(done.values()) if done else None
        return self.write_state(steps=["build", "review", "test", "ship"], finished=done,
                                last_done_at=last, **fields)


class NamedStepsTest(ProgressHome):
    def test_bar_names_the_current_step_and_counts_finished_ones(self):
        r = self.wp("set", "release 2.4", "--steps", "build,review,test,ship", "--eta", "1h30m")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("0/4 ▸ build", self.plain())
        self.wp("step")
        self.assertIn("1/4 ▸ review", self.plain())
        self.wp("done", "test")  # out of order: review stays the current step
        line = self.plain()
        self.assertIn("2/4 ▸ review", line)
        self.assertIn("50%", line)
        self.assertEqual([s["done"] for s in self.status()["steps"]], [True, False, True, False])

    def test_last_step_finishes_the_bar(self):
        self.wp("set", "release 2.4", "--steps", "build,ship")
        self.wp("step", "build")
        self.wp("step", "ship")
        view = self.status()
        self.assertTrue(view["finished"])
        self.assertIsNone(view["current_step"])
        self.assertEqual(view["percent"], 100)
        self.assertIn("2/2 · ✓ done in", self.plain())

    def test_step_accepts_a_unique_prefix_in_any_case(self):
        self.wp("set", "release 2.4", "--steps", "build,review,test,ship")
        self.wp("step", "REV")
        self.assertTrue(self.status()["steps"][1]["done"])

    def test_unknown_step_is_reported_without_failing_the_caller(self):
        self.wp("set", "release 2.4", "--steps", "build,ship")
        r = self.wp("step", "deploy")
        self.assertEqual(r.returncode, 0)
        self.assertIn("build, ship", r.stderr)
        self.assertEqual(self.status()["done"], 0)

    def test_duplicate_step_names_are_a_usage_error(self):
        r = self.wp("set", "x", "--steps", "a,b,a")
        self.assertEqual(r.returncode, 2)
        self.assertFalse(os.path.exists(self.state_path()))

    def test_set_again_with_the_same_label_replans_and_keeps_the_clock(self):
        self.four_steps({"build": 1200}, ago_started=3600)
        self.wp("set", "release 2.4", "--steps", "build,review,test,ship,docs")
        view = self.status()
        self.assertEqual((view["done"], view["total"]), (1, 5))
        self.assertGreaterEqual(view["elapsed_s"], 3590)
        self.wp("set", "release 2.4", "--steps", "build,ship", "--restart")
        view = self.status()
        self.assertEqual((view["done"], view["total"]), (0, 2))
        self.assertLess(view["elapsed_s"], 60)

    def test_unnamed_steps_count_up_like_a_plain_counter(self):
        self.wp("set", "migration", "--total", "3", "--done", "1")
        self.wp("bump", "--note", "batch 2 loaded")
        line = self.plain()
        self.assertIn("2/3", line)
        self.assertNotIn("▸", line)
        self.assertIn("batch 2 loaded", line)


class EtaBlendTest(ProgressHome):
    """build finished 40m ago and review 10m ago, 60m after the start: a
    pace of 25m a step, 10m into test, so the pace says 15m + 25m = 40m."""

    def assert_about(self, actual, expected, slack=5):
        self.assertIsNotNone(actual)
        self.assertLessEqual(abs(actual - expected), slack, f"{actual} vs {expected}")

    def test_blend_moves_weight_to_the_pace_as_steps_finish(self):
        self.four_steps({"build": 2400, "review": 600}, eta_at=self.now + 1800, eta_done=0)
        view = self.status()
        self.assertEqual(view["left_source"], "blend")
        self.assertEqual(view["pace_s_per_step"], 1500)
        self.assert_about(view["pace_left_s"], 2400)
        self.assert_about(view["estimate_left_s"], 1800)
        self.assert_about(view["left_s"], 0.5 * 2400 + 0.5 * 1800)  # 2 of 4 steps measured
        self.assertIn("~35m left", self.plain())

    def test_a_restated_estimate_starts_at_full_weight(self):
        self.four_steps({"build": 2400, "review": 600}, eta_at=self.now + 1800, eta_done=2)
        view = self.status()
        self.assertEqual(view["left_source"], "estimate")
        self.assert_about(view["left_s"], 1800)

    def test_a_passed_estimate_gives_way_to_the_pace(self):
        self.four_steps({"build": 2400, "review": 600}, eta_at=self.now - 600, eta_done=0)
        view = self.status()
        self.assertEqual(view["left_source"], "pace")
        self.assert_about(view["left_s"], 2400)

    def test_a_passed_estimate_with_no_pace_says_so(self):
        self.four_steps({}, eta_at=self.now - 600)
        view = self.status()
        self.assertEqual((view["left_source"], view["left_s"]), ("past_estimate", 0))
        self.assertIn("past estimate", self.plain())

    def test_never_negative_and_finishing_near_the_end(self):
        # last step already 1100s in against a 100s pace: nothing left to count down
        self.write_state(steps=["a", "b"], finished={"a": self.now - 1100},
                         last_done_at=self.now - 1100, ago_started=1200)
        view = self.status()
        self.assertEqual(view["left_s"], 0)
        self.assertIn("finishing", self.plain())

    def test_steps_ticked_off_right_after_set_are_not_a_pace(self):
        self.wp("set", "x", "--steps", "a,b,c,d", "--eta", "1h")
        self.wp("step")
        self.wp("step")
        view = self.status()
        self.assertEqual(view["left_source"], "estimate")
        self.assertIsNone(view["pace_s_per_step"])
        self.assert_about(view["left_s"], 3600)

    def test_time_left_is_rounded_to_what_an_estimate_can_claim(self):
        for seconds, text in ((4800, "~1h 20m left"), (2710, "~45m left"), (400, "~7m left")):
            self.four_steps({}, eta_at=self.now + seconds)
            self.assertIn(text, self.plain())


class QuietMarkTest(ProgressHome):
    def test_marked_quiet_after_twenty_minutes_without_an_update(self):
        self.four_steps({}, ago_updated=25 * 60)
        self.assertTrue(self.status()["quiet"])
        self.assertIn("⚠ quiet 25m", self.plain())

    def test_not_quiet_before_the_threshold(self):
        self.four_steps({}, ago_updated=10 * 60)
        self.assertFalse(self.status()["quiet"])
        self.assertNotIn("quiet", self.plain())

    def test_threshold_comes_from_the_environment(self):
        self.four_steps({}, ago_updated=25 * 60)
        env = {"CLAUDE_USAGE_PROGRESS_QUIET_MIN": "30"}
        self.assertFalse(self.status(env=env)["quiet"])

    def test_a_bar_can_set_its_own_threshold_or_turn_it_off(self):
        self.four_steps({}, ago_updated=50 * 60, quiet_min=45)
        self.assertTrue(self.status()["quiet"])
        self.four_steps({}, ago_updated=3 * 3600, quiet_min=0)
        self.assertFalse(self.status()["quiet"])

    def test_a_finished_bar_is_never_quiet(self):
        self.write_state(steps=["a"], finished={"a": self.now - 1500}, ago_updated=1500)
        self.assertFalse(self.status()["quiet"])


class SessionIsolationTest(ProgressHome):
    def test_two_sessions_never_see_or_change_each_others_bar(self):
        self.wp("set", "alpha", "--steps", "a,b", session="A")
        self.wp("set", "beta", "--steps", "c,d", session="B")
        a, b = self.plain(session="A"), self.plain(session="B")
        self.assertIn("alpha", a)
        self.assertNotIn("beta", a)
        self.assertIn("beta", b)
        self.assertNotIn("alpha", b)
        self.wp("step", session="A")
        self.assertEqual(self.status(session="B")["done"], 0)
        self.wp("clear", session="A")
        self.assertEqual(self.plain(session="A"), "")
        self.assertIn("beta", self.plain(session="B"))

    def test_the_session_id_defaults_to_the_one_claude_code_sets(self):
        r = self.wp("set", "from env", session=None, env={"CLAUDE_CODE_SESSION_ID": "envS"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(self.state_path("envS")))
        self.assertIn("from env", self.plain(session="envS"))

    def test_no_session_id_is_a_usage_error_but_segment_stays_silent(self):
        self.assertEqual(self.wp("set", "x", session=None).returncode, 2)
        r = self.wp("segment", session=None)
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_a_file_carrying_another_sessions_id_draws_nothing(self):
        self.four_steps({})
        with open(self.state_path("s1")) as f:
            state = json.load(f)
        state["session"] = "someone-else"
        with open(self.state_path("s1"), "w") as f:
            json.dump(state, f)
        self.assertEqual(self.segment(), "")

    def test_a_session_id_cannot_point_outside_the_state_directory(self):
        self.wp("set", "x", session="../../escape")
        written = [os.path.join(root, name) for root, _, names in os.walk(self.home)
                   for name in names if "escape" in name]
        self.assertEqual(written, [self.state_path("escape")])

    def test_set_prunes_bars_untouched_for_a_week(self):
        self.four_steps({}, session="old")
        week_ago = self.now - 8 * 86400
        os.utime(self.state_path("old"), (week_ago, week_ago))
        self.four_steps({}, session="recent")
        self.wp("set", "new", session="s1")
        self.assertFalse(os.path.exists(self.state_path("old")))
        self.assertTrue(os.path.exists(self.state_path("recent")))


class CorruptStateTest(ProgressHome):
    BAD = [b"", b"{not json", b"[1, 2]", b"\xff\xfe\x00garbage",
           b'{"label": "x", "total": "many", "started_at": 1}',
           b'{"label": "x", "steps": "build", "started_at": 1}',
           b'{"label": "x", "total": 3}',
           b'{"label": "' + b"x" * 70000 + b'", "started_at": 1}']

    def test_an_unreadable_file_renders_nothing_and_breaks_nothing(self):
        for raw in self.BAD:
            with open(self.state_path(), "wb") as f:
                f.write(raw)
            r = self.wp("segment")
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""), raw[:40])
            view = self.status()
            self.assertEqual((view["exists"], view["readable"]), (True, False))
            r = self.wp("step")
            self.assertEqual(r.returncode, 0)
            self.assertIn("unreadable", r.stderr)


class ColourAndGlyphTest(ProgressHome):
    def setUp(self):
        super().setUp()
        self.four_steps({"build": 600}, ago_updated=30 * 60, note="migrating fixtures",
                        eta_at=self.now + 1800)

    def test_colour_by_default_for_the_statusline(self):
        self.assertIn("\033[", self.segment())

    def test_no_color_output_has_no_escape_codes(self):
        line = self.segment(env={"NO_COLOR": "1"})
        self.assertNotIn("\033", line)
        self.assertIn("⚠ quiet 30m", line)

    def test_an_empty_no_color_does_not_count(self):
        self.assertIn("\033[", self.segment(env={"NO_COLOR": ""}))

    def test_ascii_fallback_uses_only_ascii(self):
        for extra, env in ((["--ascii"], None), ([], {"CLAUDE_USAGE_PROGRESS_ASCII": "1"})):
            line = self.plain(*extra, env=env)
            self.assertTrue(line.isascii(), line)
            self.assertRegex(line, r"\[#+-+\] 25% \| 1/4 > review \|")
            self.assertIn("! quiet 30m", line)

    def test_what_a_command_prints_back_is_plain_when_piped(self):
        r = self.wp("note", "still migrating")
        self.assertIn("still migrating", r.stdout)
        self.assertNotIn("\033", r.stdout)


class WidthTest(ProgressHome):
    def setUp(self):
        super().setUp()
        self.four_steps({"build": 600}, note="a long note " * 12)

    def cells(self, line):
        return len(BAR_GLYPHS.findall(line))

    def test_width_option_sets_the_bar_length(self):
        self.assertEqual(self.cells(self.plain("--width", "20")), 20)

    def test_width_from_the_environment(self):
        self.assertEqual(self.cells(self.plain(env={"CLAUDE_USAGE_PROGRESS_WIDTH": "10"})), 10)

    def test_bar_follows_the_terminal_and_the_line_never_wraps(self):
        self.assertEqual(self.cells(self.plain(env={"COLUMNS": "160"})), 16)
        line = self.plain(env={"COLUMNS": "100"})
        self.assertEqual(self.cells(line), 10)
        self.assertLessEqual(len(line), 100 - 4)
        self.assertTrue(line.endswith("…"), line)  # the note is trimmed to fit
        narrow = self.plain(env={"COLUMNS": "70"})
        self.assertLessEqual(len(narrow), 70 - 4)
        self.assertNotIn("a long note", narrow)  # and dropped when too little of it fits


class FinishedAndStaleTest(ProgressHome):
    def test_finished_shows_the_total_time_then_hides(self):
        self.write_state(steps=["a", "b"], finished={"a": self.now - 1800, "b": self.now - 600},
                         finished_at=self.now - 600, ago_started=6720, ago_updated=600)
        line = self.segment()
        self.assertIn("\033[32m✓ done in 1h 42m", line)
        self.write_state(steps=["a", "b"], finished={"a": self.now - 3600, "b": self.now - 1900},
                         finished_at=self.now - 1900, ago_updated=1900)
        self.assertEqual(self.segment(), "")
        view = self.status()
        self.assertEqual((view["visible"], view["hidden_reason"]), (False, "finished"))
        self.assertTrue(self.status(env={"CLAUDE_USAGE_PROGRESS_DONE_MIN": "60"})["visible"])

    def test_a_bar_untouched_for_eight_hours_hides(self):
        self.four_steps({}, ago_started=10 * 3600, ago_updated=9 * 3600)
        self.assertEqual(self.segment(), "")
        self.assertEqual(self.status()["hidden_reason"], "stale")
        env = {"CLAUDE_USAGE_PROGRESS_STALE_HOURS": "12"}
        self.assertNotEqual(self.segment(env=env), "")


class EtaCommandTest(ProgressHome):
    def test_durations_in_the_forms_people_write_them(self):
        self.wp("set", "x", "--total", "2")
        for text, seconds in (("1h30m", 5400), ("90", 5400), ("1.5h", 5400), ("45min", 2700),
                              ("1h 5m", 3900)):
            self.assertEqual(self.wp("eta", text).returncode, 0)
            self.assertLessEqual(abs(self.status()["estimate_left_s"] - seconds), 5, text)
        self.wp("eta", "off")
        self.assertIsNone(self.status()["left_source"])
        self.assertEqual(self.wp("eta", "soon").returncode, 2)

    def test_restating_the_estimate_gives_it_full_weight_again(self):
        # two quick steps would pull a blend well under the new figure
        self.four_steps({"build": 2400, "review": 600}, eta_at=self.now + 60, eta_done=0)
        self.wp("eta", "30m")
        view = self.status()
        self.assertEqual(view["left_source"], "estimate")
        self.assertLessEqual(abs(view["left_s"] - 1800), 5)
        self.wp("step", "--eta", "10m")  # the same holds when a step restates it
        self.assertEqual(self.status()["left_source"], "estimate")


class JsonShapeTest(ProgressHome):
    KEYS = {
        "session": str, "exists": bool, "readable": bool, "label": str, "note": str,
        "done": int, "total": int, "percent": int, "steps": list, "current_step": str,
        "started_at": str, "updated_at": str, "eta_at": str, "finished_at": type(None),
        "elapsed_s": int, "left_s": int, "left_source": str, "estimate_left_s": int,
        "pace_s_per_step": int, "pace_left_s": int, "quiet": bool, "quiet_s": int,
        "quiet_after_s": int, "finished": bool, "visible": bool,
        "hidden_reason": type(None), "segment": str,
    }

    def test_status_json_has_a_stable_shape(self):
        self.four_steps({"build": 1200}, eta_at=self.now + 1800, note="n")
        view = self.status()
        self.assertEqual(set(view), set(self.KEYS))
        for key, kind in self.KEYS.items():
            self.assertIsInstance(view[key], kind, key)
        self.assertEqual(set(view["steps"][0]), {"name", "done", "finished_at"})
        self.assertNotIn("\033", view["segment"])

    def test_no_bar_is_a_short_answer_not_an_error(self):
        self.assertEqual(self.status(), {"session": "s1", "exists": False, "readable": False})


@unittest.skipIf(fcntl is None, "no advisory file locks on this platform")
class ParallelStepsTest(ProgressHome):
    def test_lanes_finishing_at_the_same_moment_all_count(self):
        lanes = [f"lane{i}" for i in range(8)]
        self.wp("set", "lanes", "--steps", ",".join(lanes))
        env = progress_env(self.home)
        procs = [subprocess.Popen([sys.executable, PROGRESS, "step", lane, "--session", "s1"],
                                  env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                 for lane in lanes]
        for p in procs:
            p.wait(timeout=30)
        self.assertEqual(self.status()["done"], len(lanes))


class StatuslineRowTest(ProgressHome):
    """The real statusline.py and usage_common.py, copied beside stub quota
    and workload scripts so a render touches nothing outside this HOME."""

    def setUp(self):
        super().setUp()
        self.app = os.path.join(self.home, "app")
        os.makedirs(self.app)
        for name in ("statusline.py", "usage_common.py", "work-progress.py"):
            shutil.copy2(os.path.join(BIN, name), self.app)
        with open(os.path.join(self.app, "usage-statusline.py"), "w") as f:
            f.write("import sys\nsys.stdin.read()\nprint('quota line')\n")
        with open(os.path.join(self.app, "workload-gauge.py"), "w") as f:
            f.write("print('workload line')\n")

    def render(self, session="s1"):
        payload = json.dumps({"session_id": session, "transcript_path": None})
        r = subprocess.run([sys.executable, os.path.join(self.app, "statusline.py")],
                           input=payload, capture_output=True, text=True, encoding="utf-8",
                           env=progress_env(self.home), cwd=self.home, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.split("\n")

    def test_the_row_appears_only_for_the_session_that_set_it(self):
        self.assertEqual(len(self.render()), 2)
        self.wp("set", "release 2.4", "--steps", "build,ship", session="other",
                script=os.path.join(self.app, "work-progress.py"))
        self.assertEqual(len(self.render()), 2)
        self.wp("set", "release 2.4", "--steps", "build,ship",
                script=os.path.join(self.app, "work-progress.py"))
        lines = self.render()
        self.assertEqual(len(lines), 3)
        self.assertIn("release 2.4", lines[2])
        self.assertIn("0/2 ▸ build", lines[2])

    def test_a_corrupt_bar_leaves_the_other_lines_alone(self):
        with open(self.state_path(), "w") as f:
            f.write("{broken")
        lines = self.render()
        self.assertEqual(len(lines), 2)
        self.assertIn("quota line", lines[0])


class PaletteTest(ProgressHome):
    """0.22.1: the row is one calm hue with nothing dimmed by default, so
    it reads as a single quiet line on a light terminal and a dark one."""

    DIM = "\x1b[2m"

    def setUp(self):
        super().setUp()
        self.four_steps({"build": 1800, "review": 600}, note="migrating fixtures",
                        eta_at=self.now + 1800, eta_set_at=self.now - 3600)

    def test_calm_is_one_hue_and_never_dims(self):
        out = self.segment()
        self.assertIn("38;5;63", out)
        self.assertNotIn(self.DIM, out)
        self.assertNotIn("38;5;173", out)

    def test_the_appearance_picks_the_tone(self):
        light = self.segment(env={"CLAUDE_USAGE_PROGRESS_APPEARANCE": "light"})
        dark = self.segment(env={"CLAUDE_USAGE_PROGRESS_APPEARANCE": "dark"})
        self.assertIn("38;5;62", light)
        self.assertIn("38;5;105", dark)
        self.assertNotIn(self.DIM, light + dark)

    def test_plain_keeps_the_terminals_own_colour(self):
        out = self.segment(env={"CLAUDE_USAGE_PROGRESS_COLOR": "plain"})
        self.assertNotIn("38;5;", out)
        self.assertNotIn(self.DIM, out)
        self.assertIn("\x1b[1m", out)

    def test_accent_is_the_warm_bar_with_dimmed_details(self):
        out = self.segment(env={"CLAUDE_USAGE_PROGRESS_COLOR": "accent"})
        self.assertIn("38;5;173", out)
        self.assertIn(self.DIM, out)

    def test_a_number_is_the_hue(self):
        out = self.segment(env={"CLAUDE_USAGE_PROGRESS_COLOR": "30"})
        self.assertIn("38;5;30", out)
        self.assertNotIn("38;5;63", out)

    def test_no_color_wins_over_every_look(self):
        for look in ("calm", "plain", "accent", "30"):
            out = self.plain(env={"CLAUDE_USAGE_PROGRESS_COLOR": look})
            self.assertNotIn("\x1b[", out, look)

    def test_every_look_draws_the_same_words(self):
        strip = re.compile("\x1b\\[[0-9;]*m")
        plain = self.plain()
        for look in ("calm", "plain", "accent", "30"):
            out = self.segment(env={"CLAUDE_USAGE_PROGRESS_COLOR": look})
            self.assertEqual(strip.sub("", out), plain, look)


if __name__ == "__main__":
    unittest.main()
