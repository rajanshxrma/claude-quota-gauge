#!/usr/bin/env python3
"""workload-gauge -- tell an I/O-bound session apart from a compute-bound one.

The whole point is the parallelize-vs-serialize call: I/O-bound work (waiting
on network, disk, an API, CI, you typing) is free to stack many-at-once
because the chips sit idle; compute-bound work (a chip -- CPU or GPU -- pegged
near 100% doing math) must run one-at-a-time because a second job just splits
the same 100%. This reads what Activity Monitor reads and prints which side
the machine is on right now, plus the advice that follows from it.

Everything here is stdlib-only and needs NO sudo -- the one usually-privileged
number, live GPU utilization, comes from `ioreg` (the IOAccelerator
PerformanceStatistics dict), not `powermetrics`, precisely so this never
prompts for a password.

Signals, and where each comes from:
  CPU busy%  -- `top -l 2` second sample (an actual ~1s interval, not the
                since-boot average the first sample would give)
  GPU util%  -- `ioreg -r -c IOAccelerator` -> "Device Utilization %"
  disk MB/s  -- `iostat` second sample (again a real 1s interval)
  net  MB/s  -- `netstat -w1 -c2` best-effort (skipped if unparseable)
  RAM        -- `memory_pressure` free%, `sysctl vm.swapusage` for swap
  load/cores -- `sysctl vm.loadavg` / `hw.ncpu`

Two independent gauges, NOT a split that sums to 100 -- because "how pegged
are the chips" and "how much data is flowing" are genuinely separate
questions, and faking a single iowait% macOS doesn't actually expose would be
a confidently-wrong number. The verdict is whichever gauge dominates.

Usage:
  workload-gauge              one reading, human-readable
  workload-gauge --watch      refresh every 1s until Ctrl-C
  workload-gauge --json       one reading as JSON
  workload-gauge --segment    compact one-line status, read from cache (instant,
                              never samples) -- for embedding in a statusline
  workload-gauge --watch-cache  background writer that keeps the cache fresh;
                              auto-spawned by --segment, self-exits when idle
"""
import json
import re
import subprocess
import sys
import time

# ---- ANSI (skipped when not a TTY or when --json) ---------------------------
_TTY = sys.stdout.isatty()
def c(code, s):
    return f"\033[{code}m{s}\033[0m" if _TTY else s
DIM, BOLD = "2", "1"
RED, YEL, GRN, CYA, MAG = "31", "33", "32", "36", "35"

# I/O throughput knee: MB/s at which the I/O gauge reads 50%. A soft saturating
# curve io/(io+KNEE) keeps the number interpretable without pretending disks
# have one true "max" -- 40 MB/s of sustained traffic is already a busy bus.
IO_KNEE_MBPS = 40.0

# ---- statusline cache plumbing ---------------------------------------------
# The lag-vs-staleness resolution: a sample costs ~1s, far too slow to run on
# every statusline render. So the render NEVER samples -- it reads CACHE_PATH
# instantly. Freshness comes from a separate background writer (--watch-cache)
# that re-samples every WRITE_EVERY seconds. The writer only lives while a
# session is actually rendering the statusline: each render bumps KEEPALIVE,
# and the writer exits once KEEPALIVE goes untouched for IDLE_EXIT seconds, so
# nothing runs when no Claude session is open. If the writer dies and the
# cache ages past STALE_AFTER, the segment shows a ⚠ rather than a stale
# number -- exactly the "we don't want it stale" guarantee.
import os
_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(_DIR, ".workload-gauge-cache.json")
KEEPALIVE = os.path.join(_DIR, ".workload-gauge-keepalive")
LOCK_PATH = os.path.join(_DIR, ".workload-gauge-writer.lock")
WRITE_EVERY = 3.0     # writer re-samples this often
STALE_AFTER = 12.0    # cache older than this => show ⚠, don't trust the number
IDLE_EXIT = 90.0      # writer self-exits after this long with no render


def _run(cmd, timeout=6):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:
        return ""


def gpu_util():
    """GPU utilization % + in-use GPU memory (bytes), no sudo. Returns (pct, mem)
    or (None, None) if this machine's IOAccelerator doesn't report it."""
    out = _run(["ioreg", "-r", "-c", "IOAccelerator"])
    m = re.search(r'"Device Utilization %"=(\d+)', out)
    mem = re.search(r'"In use system memory"=(\d+)', out)
    return (int(m.group(1)) if m else None,
            int(mem.group(1)) if mem else None)


def cpu_and_disk():
    """Kick off the two ~1s interval samplers (top, iostat) concurrently so the
    whole reading costs ~1s wall, not ~2s. Returns (user, sys, idle, disk_mbps)."""
    top = subprocess.Popen(["top", "-l", "2", "-n", "0", "-s", "1"],
                           stdout=subprocess.PIPE, text=True)
    ios = subprocess.Popen(["iostat", "-d", "-w", "1", "-c", "2"],
                           stdout=subprocess.PIPE, text=True)
    top_out, _ = top.communicate(timeout=8)
    ios_out, _ = ios.communicate(timeout=8)

    user = sysu = idle = 0.0
    # Second "CPU usage" line == the real interval; the first is since boot.
    cpu_lines = [l for l in top_out.splitlines() if "CPU usage" in l]
    if cpu_lines:
        last = cpu_lines[-1]
        for val, key in re.findall(r"([\d.]+)% (\w+)", last):
            if key == "user": user = float(val)
            elif key == "sys": sysu = float(val)
            elif key == "idle": idle = float(val)

    # iostat: header lines then two data rows; each disk contributes 3 numbers
    # (KB/t, tps, MB/s), so MB/s is every 3rd value. Sum the last data row.
    disk_mbps = 0.0
    data_rows = [l for l in ios_out.splitlines()
                 if re.match(r"^\s*[\d.]", l)]
    if data_rows:
        nums = re.findall(r"[\d.]+", data_rows[-1])
        disk_mbps = sum(float(n) for i, n in enumerate(nums) if i % 3 == 2)
    return user, sysu, idle, disk_mbps


def net_mbps():
    """Best-effort aggregate network MB/s. netstat's format varies; if we can't
    trust the parse we return 0.0 rather than a made-up number."""
    out = _run(["netstat", "-w", "1", "-c", "2"], timeout=5)
    rows = [l for l in out.splitlines() if re.match(r"^\s*\d", l)]
    if not rows:
        return 0.0
    nums = re.findall(r"\d+", rows[-1])
    # Aggregate layout: in[pkts errs bytes] out[pkts errs bytes] colls
    if len(nums) >= 6:
        return (int(nums[2]) + int(nums[5])) / 1e6
    return 0.0


def mem_status():
    """(free_pct, swap_used_mb)."""
    free_pct = None
    mp = _run(["memory_pressure"], timeout=5)
    m = re.search(r"free percentage:\s*(\d+)%", mp)
    if m:
        free_pct = int(m.group(1))
    swap_mb = None
    sw = _run(["sysctl", "-n", "vm.swapusage"])
    m = re.search(r"used\s*=\s*([\d.]+)M", sw)
    if m:
        swap_mb = float(m.group(1))
    return free_pct, swap_mb


def loadavg():
    out = _run(["sysctl", "-n", "vm.loadavg"])
    nums = re.findall(r"[\d.]+", out)
    return [float(n) for n in nums[:3]] if len(nums) >= 3 else [0, 0, 0]


def top_procs(n=4):
    """The point of naming them: 'compute-bound' is only actionable if you know
    WHICH job to serialize. `ps -r` sorts by CPU; %cpu can exceed 100 because
    it sums across cores (a multithreaded job spanning 4 cores reads ~400%),
    which is itself the tell that a job is genuinely chewing compute."""
    out = _run(["ps", "-Aceo", "pcpu,rss,comm", "-r"])
    procs = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pcpu = float(parts[0])
            rss_mb = int(parts[1]) / 1024
        except ValueError:
            continue
        if pcpu < 1.0:
            break  # already sorted desc; nothing below is worth showing
        procs.append((pcpu, rss_mb, parts[2].strip()))
        if len(procs) >= n:
            break
    return procs


def ncpu():
    out = _run(["sysctl", "-n", "hw.ncpu"])
    try:
        return int(out.strip())
    except ValueError:
        return 0


def sample():
    user, sysu, idle, disk = cpu_and_disk()
    gpu, gpu_mem = gpu_util()
    net = net_mbps()
    free_pct, swap_mb = mem_status()
    la = loadavg()
    cores = ncpu()
    procs = top_procs()

    cpu_busy = round(user + sysu, 1)
    # Compute gauge = the dominant chip peg. Whichever chip is closest to 100%
    # is the one a second job would have to fight for.
    chip = max(cpu_busy, gpu or 0)
    chip_name = "GPU" if (gpu or 0) >= cpu_busy else "CPU"
    # I/O gauge = throughput saturation, soft-kneed so it's readable.
    io_flow = disk + net
    io_pct = round(100 * io_flow / (io_flow + IO_KNEE_MBPS), 0)

    # Verdict. A chip clearly pegged and clearly above I/O => serialize. Real
    # data flowing and out-pacing the chips => parallelize. Otherwise the
    # machine just isn't saturated on either axis.
    if chip >= 65 and chip >= io_pct + 15:
        verdict, klass = f"COMPUTE-BOUND ({chip_name})", "compute"
    elif io_pct >= 40 and io_pct > chip:
        verdict, klass = "I/O-BOUND", "io"
    elif chip < 15 and io_pct < 15:
        verdict, klass = "IDLE", "idle"
    else:
        verdict, klass = "MIXED / not saturated", "mixed"

    return {
        "class": klass, "verdict": verdict,
        "compute_pct": round(chip, 0), "chip": chip_name,
        "io_pct": io_pct,
        "cpu_user": round(user, 0), "cpu_sys": round(sysu, 0),
        "cpu_idle": round(idle, 0), "cpu_busy": cpu_busy,
        "gpu_pct": gpu, "gpu_mem_gb": round(gpu_mem / 1e9, 1) if gpu_mem else None,
        "disk_mbps": round(disk, 1), "net_mbps": round(net, 1),
        "ram_free_pct": free_pct, "swap_mb": swap_mb,
        "load": la, "cores": cores,
        "top": [{"cpu": round(p, 0), "rss_mb": round(r, 0), "name": nm}
                for p, r, nm in procs],
    }


def bar(pct, width=24):
    fill = int(round((pct or 0) / 100 * width))
    return "█" * fill + "░" * (width - fill)


def advice(s):
    if s["class"] == "compute":
        return f"Serialize: run one {s['chip']}-heavy job at a time -- a second just splits the same 100%."
    if s["class"] == "io":
        return "Parallelize freely: the chips are idle, so overlapping I/O work is basically free."
    if s["class"] == "idle":
        return "Nothing's saturated -- start whatever you like."
    return "Neither axis is pegged -- room to add work, but watch which gauge climbs first."


def color_for(pct):
    return RED if pct >= 80 else YEL if pct >= 50 else GRN


def render(s):
    la = s["load"]
    cores = s["cores"] or 1
    over = la[0] > cores
    load_str = f"load {la[0]:.1f} / {cores} cores"
    if over:
        load_str = c(YEL, load_str + "  ⚠ oversubscribed")
    else:
        load_str = c(DIM, load_str)

    lines = []
    lines.append(f"  {c(BOLD, 'WORKLOAD GAUGE')}   {load_str}")
    lines.append("")
    lines.append(f"  Compute  {c(color_for(s['compute_pct']), bar(s['compute_pct']))}  "
                 f"{c(BOLD, str(int(s['compute_pct'])) + '%'):>3}   {c(DIM, s['chip'] + '-side')}")
    lines.append(f"  I/O      {c(color_for(s['io_pct']), bar(s['io_pct']))}  "
                 f"{c(BOLD, str(int(s['io_pct'])) + '%')}")
    lines.append("")
    vcolor = {"compute": MAG, "io": CYA, "idle": DIM, "mixed": YEL}[s["class"]]
    lines.append(f"  → {c(vcolor, c(BOLD, s['verdict']))}.  {advice(s)}")
    lines.append("")

    gpu_str = f"{s['gpu_pct']}%" if s["gpu_pct"] is not None else "n/a"
    if s["gpu_mem_gb"]:
        gpu_str += f"  ({s['gpu_mem_gb']} GB)"
    lines.append(c(DIM, f"  CPU  {int(s['cpu_user'])}% user · {int(s['cpu_sys'])}% sys "
                       f"· {int(s['cpu_idle'])}% idle        GPU  {gpu_str}"))

    ram_bits = []
    if s["ram_free_pct"] is not None:
        ram_bits.append(f"{s['ram_free_pct']}% free")
    if s["swap_mb"]:
        ram_bits.append(f"swap {s['swap_mb']/1024:.1f} GB used")
    ram_line = "  RAM  " + " · ".join(ram_bits)
    heavy_swap = (s["swap_mb"] or 0) > 1024 or (s["ram_free_pct"] or 100) < 20
    if heavy_swap:
        ram_line = c(RED, ram_line + "   ⚠ in swap -- RAM is the real bottleneck; don't stack big models")
    else:
        ram_line = c(DIM, ram_line)
    lines.append(ram_line)

    if s["top"]:
        lines.append("")
        lines.append(c(DIM, "  who's eating it:"))
        for p in s["top"]:
            over = "" if p["cpu"] <= 100 else c(YEL, f"  ({int(p['cpu']//100)+1} cores)")
            lines.append(c(color_for(min(p["cpu"], 100)),
                          f"    {int(p['cpu']):>4}% cpu")
                         + c(DIM, f"  {int(p['rss_mb']):>5} MB  {p['name']}") + over)
    return "\n".join(lines)


# ---- cache read/write + writer lifecycle -----------------------------------
def write_cache(s):
    s = dict(s, ts=time.time())
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, CACHE_PATH)  # atomic; a reader never sees a half-written file


def read_cache():
    """Returns (data, age_seconds) or (None, None) if there's no cache yet."""
    try:
        with open(CACHE_PATH) as f:
            d = json.load(f)
        return d, time.time() - d.get("ts", 0)
    except Exception:
        return None, None


def touch_keepalive():
    try:
        with open(KEEPALIVE, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def keepalive_age():
    try:
        return time.time() - float(open(KEEPALIVE).read().strip())
    except Exception:
        return 1e9


def ensure_writer():
    """Spawn a detached background writer if one isn't already holding the lock.
    Cheap to call every render: the flock try-acquire below is the real guard."""
    import fcntl
    try:
        f = open(LOCK_PATH, "w")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        f.close()  # nobody holds it -> no writer alive -> start one
    except (IOError, OSError):
        return  # a writer already holds the lock; nothing to do
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--watch-cache"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


def watch_cache_loop():
    """The background writer. Holds the lock for its whole life so only one runs,
    refreshes the cache every WRITE_EVERY, and exits once no render has touched
    the keepalive for IDLE_EXIT seconds."""
    import fcntl
    f = open(LOCK_PATH, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        return  # lost the race to another writer
    touch_keepalive()  # the spawner is about to render; count that as activity
    try:
        while keepalive_age() < IDLE_EXIT:
            write_cache(sample())  # sample() itself costs ~1s
            time.sleep(WRITE_EVERY)
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def segment():
    """One compact, colored statusline line, read purely from cache -- never
    samples, so it can't lag a render. Bumps the keepalive and makes sure the
    background writer is alive. ANSI is forced on (statuslines render it even
    though stdout isn't a TTY here)."""
    touch_keepalive()
    ensure_writer()
    d, age = read_cache()

    def sc(code, s):
        return f"\033[{code}m{s}\033[0m"

    if d is None:
        return sc(DIM, "⚙ workload …")  # first render before the writer's first sample
    if age is not None and age > STALE_AFTER:
        return sc(YEL, f"⚙ workload ⚠ stale ({int(age)}s)")

    glyph = {"compute": "⚙", "io": "⇄", "idle": "·", "mixed": "◐"}[d["class"]]
    verb = {"compute": f"serialize·{d['chip']}", "io": "parallelize",
            "idle": "idle", "mixed": "mixed"}[d["class"]]
    vcolor = {"compute": MAG, "io": CYA, "idle": DIM, "mixed": YEL}[d["class"]]
    comp = sc(color_for(d["compute_pct"]), f"{int(d['compute_pct'])}%")
    io = sc(color_for(d["io_pct"]), f"{int(d['io_pct'])}%")
    out = f"{glyph} compute {comp} io {io} {sc(vcolor, '→ ' + verb)}"
    if (d.get("swap_mb") or 0) > 1024 or (d.get("ram_free_pct") or 100) < 20:
        out += "  " + sc(RED, "⚠swap")
    return out


def main():
    args = sys.argv[1:]
    if "--watch-cache" in args:
        watch_cache_loop()
        return
    if "--segment" in args:
        print(segment())
        return
    if "--json" in args:
        print(json.dumps(sample(), indent=2))
        return
    if "--watch" in args:
        try:
            while True:
                s = sample()
                sys.stdout.write("\033[2J\033[H" if _TTY else "")
                print(render(s))
                sys.stdout.flush()
                time.sleep(1)  # sample() itself already costs ~1s
        except KeyboardInterrupt:
            print()
        return
    print(render(sample()))


if __name__ == "__main__":
    main()
