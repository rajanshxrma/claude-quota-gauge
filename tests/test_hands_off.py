"""Tests for 0.25.0: the hands-off row (usage_common.hands_off_*,
bin/hands-off-flag.py). Every flag here is written to a temporary file;
the real ~/.claude/hands-off.json is never touched.

Run with:  python3 -m unittest discover -s tests -v
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "bin"))
import usage_common as U  # noqa: E402

ANSI = re.compile("\x1b\\[[0-9;]*m")


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class HandsOff(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "hands-off.json")

    def load(self, now=None):
        return U.hands_off_load(now, path=self.path)

    def test_live_flag_reads_and_formats(self):
        U.hands_off_set("UI test is dragging across the app", 6, path=self.path)
        state = self.load()
        self.assertIsNotNone(state)
        row = U.fmt_hands_off(state, color=True)
        self.assertIn("\x1b[1;97;41m", row)
        self.assertNotIn("\x1b[5", row)  # never blinks
        self.assertEqual(ANSI.sub("", row).strip(),
                         "✋ HANDS OFF THE MAC — UI test is dragging across the app · about 6 min left")

    def test_time_left_wording(self):
        now = time.time()
        state = {"reason": "r", "until_ts": now + 30}
        self.assertTrue(U.fmt_hands_off(state, now, color=False).endswith("under a minute left"))
        state["until_ts"] = now - 5
        self.assertTrue(U.fmt_hands_off(state, now, color=False).endswith("running past its estimate"))
        state["paused"] = True
        self.assertEqual(U.fmt_hands_off(state, now, color=False), "✋ HANDS OFF THE MAC — r")
        self.assertEqual(U.fmt_hands_off({"reason": "r", "until_ts": now + 300}, now, color=False, ascii_only=True),
                         "!! HANDS OFF THE MAC -- r - about 5 min left")

    def test_dead_owner_is_dropped_and_removed(self):
        U.hands_off_set("r", 10, pid=dead_pid(), path=self.path)
        self.assertIsNone(self.load())
        self.assertFalse(os.path.exists(self.path))

    def test_long_past_estimate_is_dropped(self):
        U.hands_off_set("r", 1, path=self.path)
        self.assertIsNotNone(self.load(time.time() + 25 * 60))
        self.assertIsNone(self.load(time.time() + 32 * 60))
        self.assertFalse(os.path.exists(self.path))

    def test_ownerless_flag_lives_on_time_alone(self):
        U.hands_off_set("r", 5, pid=0, path=self.path)
        self.assertIsNone(json.load(open(self.path))["pid"])
        self.assertIsNotNone(self.load())

    def test_clear_only_takes_its_own_flag(self):
        U.hands_off_set("r", 5, path=self.path)
        self.assertFalse(U.hands_off_clear(pid=os.getpid() + 1, path=self.path))
        self.assertTrue(os.path.exists(self.path))
        self.assertTrue(U.hands_off_clear(pid=os.getpid(), path=self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_restating_keeps_since(self):
        first = U.hands_off_set("r", 5, path=self.path)
        second = U.hands_off_set("r paused", 5, paused=True, path=self.path)
        self.assertEqual(first["since"], second["since"])
        self.assertTrue(self.load()["paused"])

    def test_unreadable_file_is_no_row(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertIsNone(self.load())

    def test_no_file_is_no_row(self):
        self.assertIsNone(self.load())


if __name__ == "__main__":
    unittest.main()
