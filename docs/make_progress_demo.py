#!/usr/bin/env python3
"""Redraws docs/progress-demo.gif from the real renderer.

    python3 docs/make_progress_demo.py            # writes docs/progress-demo.gif
    python3 docs/make_progress_demo.py preview.png # one still, all frames stacked

Each frame shows the same bar on a light terminal and on a dark one, so a
change to the colours is judged on both before it ships. Needs Pillow
(`pip install pillow`); nothing else in the repo does.
"""
import os
import re
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "bin"))
import usage_common as uc  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

XTERM = {173: (215, 135, 95), 63: (95, 95, 255), 62: (95, 95, 215), 105: (135, 135, 255)}
BASIC = {"32": {"light": (30, 130, 50), "dark": (90, 200, 110)},
         "33": {"light": (150, 120, 0), "dark": (225, 190, 70)}}
THEMES = {"light": {"bg": (250, 249, 245), "fg": (28, 28, 30), "dim": (150, 150, 150)},
          "dark": {"bg": (24, 24, 26), "fg": (235, 235, 238), "dim": (120, 120, 126)}}
SGR = re.compile("\x1b\\[([0-9;]*)m")


def font(size):
    for path in ("/System/Library/Fonts/Menlo.ttc", "/System/Library/Fonts/SFNSMono.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def spans(line, theme):
    """(text, colour, bold) runs from a line carrying SGR codes."""
    colour, bold, pos, out = THEMES[theme]["fg"], False, 0, []
    for m in SGR.finditer(line):
        if m.start() > pos:
            out.append((line[pos:m.start()], colour, bold))
        pos = m.end()
        codes = [c for c in m.group(1).split(";") if c] or ["0"]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == "0":
                colour, bold = THEMES[theme]["fg"], False
            elif c == "1":
                bold = True
            elif c == "2":
                colour = THEMES[theme]["dim"]
            elif c in BASIC:
                colour = BASIC[c][theme]
            elif c == "38" and codes[i + 1:i + 2] == ["5"]:
                colour = XTERM.get(int(codes[i + 2]), THEMES[theme]["fg"])
                i += 2
            i += 1
    if pos < len(line):
        out.append((line[pos:], colour, bold))
    return out


def frame(view, caption, pulse=None, size=15, width=1180):
    regular, pad, row = font(size), 18, size + 14
    image = Image.new("RGB", (width, pad * 2 + row * 4 + 10), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    y = 0
    for theme in ("light", "dark"):
        os.environ["CLAUDE_USAGE_PROGRESS_APPEARANCE"] = theme
        height = pad + row * 2
        draw.rectangle([0, y, width, y + height], fill=THEMES[theme]["bg"])
        draw.text((pad, y + 8), f"{caption}  ·  {theme} terminal", font=font(size - 3),
                  fill=THEMES[theme]["dim"])
        x = pad
        for text, colour, bold in spans(uc.fmt_work_progress(view, columns=140, live=True, pulse=pulse), theme):
            draw.text((x, y + 8 + row), text, font=regular, fill=colour,
                      stroke_width=1 if bold else 0, stroke_fill=colour)
            x += draw.textlength(text, font=regular)
        y += height + 5
    return image


def views():
    now = time.time()
    base = {"session": "demo", "label": "release 2.4", "steps": ["build", "review", "test", "ship"],
            "started_at": now, "updated_at": now, "eta_at": now + 5400, "eta_set_at": now,
            "note": "building"}
    def at(minutes, finished, note, quiet=0, eta=None):
        t = now + minutes * 60
        state = dict(base, note=note, updated_at=t - quiet * 60,
                     finished={k: now + v * 60 for k, v in finished.items()})
        if finished:
            state["last_done_at"] = max(state["finished"].values())
        if eta is not None:
            state["eta_at"], state["eta_set_at"] = t + eta * 60, t
        return uc.work_progress_view(uc.work_progress_normalize(state, "demo"),
                                     datetime.fromtimestamp(t, timezone.utc))
    running = "live: the clock counts seconds, the fill creeps through the running step, the pulse turns"
    return [(at(1, {}, "building"), "set: four named steps, 1h 30m estimate", "◐"),
            (at(12, {}, "building"), running, "◓"),
            (at(12 + 2 / 60, {}, "building"), running, "◑"),
            (at(12 + 4 / 60, {}, "building"), running, "◒"),
            (at(24, {"build": 22}, "reviewing"), "a step finishes", "◐"),
            (at(58, {"build": 22, "review": 51}, "migrating fixtures"), "halfway", "◓"),
            (at(80, {"build": 22, "review": 51}, "migrating fixtures", quiet=21),
             "nothing has moved for 21 minutes: the pulse rests", "○"),
            (at(102, {"build": 22, "review": 51, "test": 90, "ship": 102}, ""), "done", None)]


def main():
    frames = [frame(view, caption, pulse) for view, caption, pulse in views()]
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "progress-demo.gif")
    if target.endswith(".png"):
        sheet = Image.new("RGB", (frames[0].width, sum(f.height + 6 for f in frames)), (255, 255, 255))
        y = 0
        for f in frames:
            sheet.paste(f, (0, y)); y += f.height + 6
        sheet.save(target)
    else:
        # The three live frames tick at the status line's own 2 second pace.
        durations = [2200, 1000, 1000, 1000, 2200, 2200, 2200, 2200]
        frames[0].save(target, save_all=True, append_images=frames[1:], duration=durations, loop=0)
    print("wrote", target)


if __name__ == "__main__":
    main()
