#!/usr/bin/env python3
"""oracle-keepalive - keep an Oracle Cloud Always Free instance out of the idle bucket.

Oracle may reclaim an Always Free compute instance that looks idle over a
7-day window.  Two cheap, polite knobs are used here:

* CPU - short bursts of a tight integer loop running in SCHED_IDLE worker
        processes, sized so that sampled CPU utilization stays above the 20%
        line without ever competing with real work.
* MEM - anonymous (not tmpfs) mmap pages that are touched once and held, so
        "used" memory stays above 20% on shapes where memory counts (A1).

Standard library only.  No network access, no subprocesses, no shell-outs.
The only files written are inside the state directory.
"""

from __future__ import annotations

import argparse
import math
import mmap
import multiprocessing
import os
import platform
import random
import signal
import sys
import time

PROG = "oracle-keepalive"
VERSION = "1.0.0"

DEFAULT_STATE_DIR = "/var/lib/oracle-keepalive"
SAMPLE_FILE = "samples.csv"
SAMPLE_HEADER = "epoch,cpu_util_pct,mem_util_pct,steal_pct,held_mb,burning"
SAMPLE_RETENTION_DAYS = 7
SAMPLE_RETENTION_SECONDS = SAMPLE_RETENTION_DAYS * 86400

# The idle test needs more than 5% of samples above the 20% line, so a duty
# cycle never goes below 6%.
DUTY_MIN, DUTY_MAX = 0.06, 0.40
BURST_MIN, BURST_MAX = 60.0, 600.0
TARGET_MIN, TARGET_MAX = 0.22, 0.40
MAXHOLD_MIN, MAXHOLD_MAX = 0.05, 0.40
FLOOR_MIN, FLOOR_MAX = 0.02, 0.50
STEP_MIN_MB, STEP_MAX_MB = 8, 1024
TICK_MIN, TICK_MAX = 1, 60
SAMPLE_MIN, SAMPLE_MAX = 10, 3600
GROWTH_LIMIT_MB = 256.0
MEM_MIN_TOTAL_KB = 2 * 1024 * 1024  # memory hold is pointless / harmful below 2 GiB
ARCH_WITH_MEM = ("aarch64", "arm64")
MODES = ("auto", "cpu", "mem", "cpu+mem", "off")
IDLE_LINE_PCT = 20.0
PAGE_SIZE = mmap.PAGESIZE

# Where the process table lives.  A module-level variable so tests can point it
# at a fake directory instead of the real /proc.
PROC_ROOT = "/proc"


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

def log(msg):
    """One line on stdout (systemd captures it into the journal)."""
    sys.stdout.write("%s %s: %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), PROG, msg))
    sys.stdout.flush()


def warn(msg):
    log("WARNING: " + msg)


# --------------------------------------------------------------------------
# /proc parsing
# --------------------------------------------------------------------------

_CPU_KEYS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq",
             "steal", "guest", "guest_nice")


def parse_meminfo(text):
    """Parse /proc/meminfo into {name: kB}.  Unknown/garbage lines are skipped."""
    out = {}
    for line in text.splitlines():
        name, sep, rest = line.partition(":")
        if not sep:
            continue
        fields = rest.split()
        if not fields:
            continue
        try:
            out[name.strip()] = int(fields[0])
        except ValueError:
            continue
    return out


def read_meminfo(path="/proc/meminfo"):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return parse_meminfo(fh.read())
    except OSError:
        return {}


def parse_proc_stat(text):
    """Return the aggregate 'cpu' line of /proc/stat as tick counters."""
    for line in text.splitlines():
        if not line.startswith("cpu "):
            continue
        fields = line.split()[1:]
        ticks = {}
        for i, key in enumerate(_CPU_KEYS):
            try:
                ticks[key] = int(float(fields[i]))
            except (IndexError, ValueError):
                ticks[key] = 0
        # 'guest' is already accounted for inside 'user', so it is not summed
        # into the total (that would double count).
        ticks["total"] = sum(ticks[k] for k in
                             ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"))
        ticks["idle_total"] = ticks["idle"] + ticks["iowait"]
        return ticks
    raise ValueError("/proc/stat: no aggregate 'cpu' line found")


def read_proc_stat(path="/proc/stat"):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return parse_proc_stat(fh.read())


def cpu_delta(prev, cur):
    """(cpu_util_pct, steal_pct) between two snapshots, both clamped to 0..100."""
    total = cur["total"] - prev["total"]
    if total <= 0:
        return 0.0, 0.0
    idle = cur["idle_total"] - prev["idle_total"]
    busy = total - idle
    if busy < 0:
        busy = 0
    steal = cur["steal"] - prev["steal"]
    if steal < 0:
        steal = 0
    cpu = 100.0 * busy / total
    st = 100.0 * steal / total
    return min(100.0, cpu), min(100.0, st)


def available_kb(meminfo):
    """MemAvailable in kB, with a conservative fallback for ancient kernels."""
    total = meminfo.get("MemTotal", 0)
    avail = meminfo.get("MemAvailable")
    if avail is None:
        avail = meminfo.get("MemFree", 0) + meminfo.get("Buffers", 0) + meminfo.get("Cached", 0)
    return max(0, min(avail, total))


def mem_util_pct(meminfo):
    """(MemTotal - MemAvailable) / MemTotal * 100, as the guest kernel sees it."""
    total = meminfo.get("MemTotal", 0)
    if total <= 0:
        return 0.0
    return 100.0 * (total - available_kb(meminfo)) / total


# --------------------------------------------------------------------------
# pure sizing math
# --------------------------------------------------------------------------

def desired_hold_mb(total_kb, avail_kb, held_kb, target, max_hold, floor):
    """How many MB we should hold so that *used* memory reaches target*total.

    `held_kb` is what we already hold; it is subtracted from the observed
    "used" figure so the answer does not feed back on itself.  Returns 0 when
    MemAvailable is below the floor (release everything immediately).
    """
    total_kb = float(total_kb)
    if total_kb <= 0:
        return 0.0
    avail_kb = max(0.0, min(float(avail_kb), total_kb))
    held_kb = max(0.0, float(held_kb))
    if (avail_kb / total_kb) < floor:
        return 0.0
    other_used_kb = (total_kb - avail_kb) - held_kb
    if other_used_kb < 0:
        other_used_kb = 0.0
    want_kb = target * total_kb - other_used_kb
    cap_kb = max_hold * total_kb
    if want_kb < 0:
        want_kb = 0.0
    if want_kb > cap_kb:
        want_kb = cap_kb
    return want_kb / 1024.0


def apply_hysteresis(current_mb, desired_mb, step_mb, growth_limit_mb=GROWTH_LIMIT_MB):
    """Dead band around the current hold, plus a ramp limit when growing.

    Shrinking is immediate (and a desired value of 0 always releases
    everything), because holding memory when the host is short on it is worse
    than a small dip in utilization.
    """
    current = max(0.0, float(current_mb))
    desired = max(0.0, float(desired_mb))
    if desired <= 0.0:
        return 0.0
    if abs(desired - current) <= float(step_mb):
        return current
    if desired > current:
        return min(desired, current + float(growth_limit_mb))
    return desired


def p95(values):
    """Nearest-rank 95th percentile (matches how the idle test reads the metric)."""
    vals = sorted(float(v) for v in values)
    if not vals:
        return 0.0
    idx = int(math.ceil(0.95 * len(vals))) - 1
    if idx < 0:
        idx = 0
    if idx > len(vals) - 1:
        idx = len(vals) - 1
    return vals[idx]


def mean(values):
    vals = [float(v) for v in values]
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

class Config(object):
    """Validated configuration.  Every value is clamped, never trusted."""

    def __init__(self, **kw):
        self.mode = "auto"
        self.cpu_duty = 0.12
        self.cpu_burst = 180.0
        self.cpu_workers = 1
        self.mem_target = 0.25
        self.mem_max_hold = 0.30
        self.mem_floor = 0.10
        self.mem_step_mb = 64.0
        self.tick = 5.0
        self.sample = 60.0
        self.state_dir = DEFAULT_STATE_DIR
        self.state_dir_valid = True
        self.notes = []
        # resolved at load time
        self.cpu_enabled = True
        self.mem_enabled = False
        self.ncpu = 1
        self.machine = "unknown"
        self.memtotal_kb = 0
        for key, value in kw.items():
            setattr(self, key, value)

    @property
    def period_seconds(self):
        if self.cpu_duty <= 0:
            return self.cpu_burst
        return self.cpu_burst / self.cpu_duty

    @property
    def idle_seconds(self):
        return max(0.0, self.period_seconds - self.cpu_burst)

    def summary(self):
        return ("mode=%s cpu=%s mem=%s duty=%.2f burst=%.0fs period=%.0fs workers=%d "
                "target=%.2f max_hold=%.2f floor=%.2f tick=%.0fs sample=%.0fs" % (
                    self.mode, self.cpu_enabled, self.mem_enabled, self.cpu_duty,
                    self.cpu_burst, self.period_seconds, self.cpu_workers,
                    self.mem_target, self.mem_max_hold, self.mem_floor,
                    self.tick, self.sample))


def resolve_mode(requested, machine, memtotal_kb):
    """Map KEEPALIVE_MODE + machine to (cpu_enabled, mem_enabled, notes).

    auto picks cpu+mem on ARM (A1) and cpu elsewhere (E2 Micro, where memory is
    not part of the idle test).  The memory half is switched off on hosts with
    less than 2 GiB of RAM even when it was forced: holding 25% of a 1 GiB box
    is more likely to hurt than to help.
    """
    notes = []
    req = (requested or "auto").strip().lower()
    if req not in MODES:
        notes.append("KEEPALIVE_MODE=%r is not one of %s; using auto" % (requested, "|".join(MODES)))
        req = "auto"
    if req == "auto":
        req = "cpu+mem" if (machine or "").lower() in ARCH_WITH_MEM else "cpu"
        notes.append("mode auto -> %s on %s" % (req, machine or "unknown"))
    if req == "off":
        return False, False, notes
    cpu = "cpu" in req
    mem = "mem" in req
    if mem and memtotal_kb and memtotal_kb < MEM_MIN_TOTAL_KB:
        mem = False
        notes.append("memory hold disabled: MemTotal %.2f GiB is below the 2 GiB minimum"
                     % (memtotal_kb / 1024.0 / 1024.0))
    if not cpu and not mem:
        notes.append("nothing to do: neither CPU nor memory hold is enabled")
    return cpu, mem, notes


def state_dir_components(path):
    """Path components of *path*, ignoring empty entries and '.'."""
    return [p for p in str(path).split("/") if p not in ("", ".")]


def state_dir_problem(path):
    """Why *path* must not be used as a state dir, or None when it is fine.

    The installer may delete the state dir on --uninstall --purge, so a value
    that is not absolute or that contains '..' is refused here instead of being
    followed.  Keeping the check in one place keeps the program and the
    installer honest about the same rule.
    """
    if not path:
        return "KEEPALIVE_STATE_DIR is empty"
    if not path.startswith("/"):
        return "KEEPALIVE_STATE_DIR=%r is not an absolute path" % (path,)
    if ".." in str(path).split("/"):
        return "KEEPALIVE_STATE_DIR=%r contains a '..' component" % (path,)
    if not state_dir_components(path):
        return "KEEPALIVE_STATE_DIR=%r has no path components" % (path,)
    return None


def _num(env, key, default, lo, hi, notes, integer=False):
    raw = (env.get(key) or "").strip()
    if not raw:
        return float(default) if not integer else int(default)
    try:
        val = float(raw)
    except ValueError:
        notes.append("%s=%r is not a number; using default %s" % (key, raw, default))
        return float(default) if not integer else int(default)
    if math.isnan(val) or math.isinf(val):
        notes.append("%s=%r is not finite; using default %s" % (key, raw, default))
        return float(default) if not integer else int(default)
    if val < lo or val > hi:
        clamped = min(max(val, lo), hi)
        notes.append("%s=%s is outside [%s, %s]; clamped to %s" % (key, val, lo, hi, clamped))
        val = clamped
    return int(round(val)) if integer else val


def load_config(env=None, machine=None, ncpu=None, meminfo=None):
    """Build a Config from environment variables (systemd EnvironmentFile).

    All arguments exist so tests can inject a fake host.
    """
    env = os.environ if env is None else env
    machine = machine if machine is not None else (platform.machine() or "unknown")
    ncpu = ncpu if ncpu is not None else (os.cpu_count() or 1)
    if ncpu < 1:
        ncpu = 1
    if meminfo is None:
        meminfo = read_meminfo()
    memtotal_kb = int(meminfo.get("MemTotal", 0) or 0)

    notes = []
    cfg = Config()
    cfg.machine = machine
    cfg.ncpu = ncpu
    cfg.memtotal_kb = memtotal_kb

    cfg.mode = (env.get("KEEPALIVE_MODE") or "auto").strip().lower() or "auto"
    cfg.cpu_duty = _num(env, "KEEPALIVE_CPU_DUTY", 0.12, DUTY_MIN, DUTY_MAX, notes)
    cfg.cpu_burst = _num(env, "KEEPALIVE_CPU_BURST_SECONDS", 180, BURST_MIN, BURST_MAX, notes)
    cfg.cpu_workers = int(_num(env, "KEEPALIVE_CPU_WORKERS", (ncpu + 1) // 2, 1, ncpu,
                               notes, integer=True))
    cfg.mem_target = _num(env, "KEEPALIVE_MEM_TARGET", 0.25, TARGET_MIN, TARGET_MAX, notes)
    cfg.mem_max_hold = _num(env, "KEEPALIVE_MEM_MAX_HOLD", 0.30, MAXHOLD_MIN, MAXHOLD_MAX, notes)
    cfg.mem_floor = _num(env, "KEEPALIVE_MEM_FLOOR", 0.10, FLOOR_MIN, FLOOR_MAX, notes)
    cfg.mem_step_mb = _num(env, "KEEPALIVE_MEM_STEP_MB", 64, STEP_MIN_MB, STEP_MAX_MB, notes, integer=True)
    cfg.tick = _num(env, "KEEPALIVE_TICK_SECONDS", 5, TICK_MIN, TICK_MAX, notes)
    cfg.sample = _num(env, "KEEPALIVE_SAMPLE_SECONDS", 60, SAMPLE_MIN, SAMPLE_MAX, notes)

    state_dir = (env.get("KEEPALIVE_STATE_DIR") or "").strip()
    cfg.state_dir = state_dir or DEFAULT_STATE_DIR
    state_problem = state_dir_problem(cfg.state_dir)
    if state_problem:
        cfg.state_dir_valid = False
        notes.append("%s; 'run' refuses to start until it is fixed" % state_problem)

    if cfg.mem_target > cfg.mem_max_hold:
        notes.append("KEEPALIVE_MEM_TARGET %.2f exceeds KEEPALIVE_MEM_MAX_HOLD %.2f; "
                     "target lowered to the cap" % (cfg.mem_target, cfg.mem_max_hold))
        cfg.mem_target = cfg.mem_max_hold

    cpu_enabled, mem_enabled, mode_notes = resolve_mode(cfg.mode, machine, memtotal_kb)
    cfg.cpu_enabled = cpu_enabled
    cfg.mem_enabled = mem_enabled
    notes.extend(mode_notes)
    cfg.notes = notes
    return cfg


# --------------------------------------------------------------------------
# samples.csv
# --------------------------------------------------------------------------

def append_sample(path, epoch, cpu_pct, mem_pct, steal_pct, held_mb, burning):
    """Append one CSV row; writes the header only when creating the file."""
    new_file = (not os.path.exists(path)) or os.path.getsize(path) == 0
    with open(path, "a", encoding="utf-8", newline="") as fh:
        if new_file:
            fh.write(SAMPLE_HEADER + "\n")
        fh.write("%d,%.2f,%.2f,%.2f,%.1f,%d\n" % (
            int(epoch), float(cpu_pct), float(mem_pct), float(steal_pct),
            float(held_mb), 1 if burning else 0))
    return True


def read_samples(path):
    """Read samples.csv into a list of dicts; bad/short lines are skipped."""
    rows = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.strip().split(",")
                if len(parts) != 6:
                    continue
                try:
                    rows.append({
                        "epoch": int(float(parts[0])),
                        "cpu": float(parts[1]),
                        "mem": float(parts[2]),
                        "steal": float(parts[3]),
                        "held_mb": float(parts[4]),
                        "burning": int(float(parts[5])),
                    })
                except ValueError:
                    continue
    except OSError:
        return []
    return rows


def trim_samples(path, now=None, keep_days=SAMPLE_RETENTION_DAYS):
    """Drop rows older than the retention window; rewrite atomically.

    Returns the number of rows removed.
    """
    rows = read_samples(path)
    if not rows:
        return 0
    now = time.time() if now is None else float(now)
    cutoff = now - keep_days * 86400
    kept = [r for r in rows if r["epoch"] >= cutoff]
    removed = len(rows) - len(kept)
    if removed <= 0:
        return 0
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(SAMPLE_HEADER + "\n")
        for r in kept:
            fh.write("%d,%.2f,%.2f,%.2f,%.1f,%d\n" % (
                r["epoch"], r["cpu"], r["mem"], r["steal"], r["held_mb"], r["burning"]))
    os.replace(tmp, path)
    return removed


# --------------------------------------------------------------------------
# burst schedule
# --------------------------------------------------------------------------

class BurstSchedule(object):
    """Burst/duty cycle with +-20% jitter on the idle gap only.

    The jitter keeps the pattern from looking like a metronome to any sampling
    scheme while leaving the mean duty cycle untouched.
    """

    def __init__(self, burst_seconds, duty, rng=None):
        self.burst = float(burst_seconds)
        self.duty = float(duty) if duty > 0 else 0.06
        self.period = self.burst / self.duty
        self.idle = max(0.0, self.period - self.burst)
        self.rng = rng if rng is not None else random.Random()

    def next_idle_gap(self):
        if self.idle <= 0:
            return 0.0
        return max(1.0, self.idle * (1.0 + self.rng.uniform(-0.2, 0.2)))

    def simulate(self, horizon_seconds):
        """Walk one burst/idle cycle at a time; return (busy_seconds, elapsed)."""
        horizon = float(horizon_seconds)
        elapsed = 0.0
        busy = 0.0
        while elapsed < horizon:
            run = min(self.burst, horizon - elapsed)
            busy += run
            elapsed += run
            if elapsed >= horizon:
                break
            elapsed = min(horizon, elapsed + self.next_idle_gap())
        return busy, elapsed


# --------------------------------------------------------------------------
# memory hold
# --------------------------------------------------------------------------

class MemoryHold(object):
    """Anonymous mmap chunks, every page touched once with a non-zero byte.

    tmpfs is deliberately not used: tmpfs pages are shared accounting and can be
    shrunk by the kernel; a private anonymous mapping shows up as ordinary used
    memory in MemTotal - MemAvailable.
    """

    def __init__(self):
        self.chunks = []  # list of [mmap_obj, nbytes]
        self._page = PAGE_SIZE

    @property
    def held_bytes(self):
        return sum(c[1] for c in self.chunks)

    @property
    def held_mb(self):
        return self.held_bytes / (1024.0 * 1024.0)

    @property
    def held_kb(self):
        return self.held_bytes / 1024.0

    def _alloc(self, nbytes):
        nbytes = int(math.ceil(float(nbytes) / self._page) * self._page)
        if nbytes <= 0:
            return None
        chunk = mmap.mmap(-1, nbytes)
        # Touch each page with a non-zero byte so it becomes resident.
        for off in range(0, nbytes, self._page):
            chunk[off] = 1
        return [chunk, nbytes]

    def grow(self, mb):
        chunk = self._alloc(float(mb) * 1024.0 * 1024.0)
        if chunk is not None:
            self.chunks.append(chunk)
        return self.held_mb

    def shrink(self, mb):
        want = float(mb) * 1024.0 * 1024.0
        while want > 0 and self.chunks:
            chunk = self.chunks.pop()
            size = chunk[1]
            if size <= want:
                try:
                    chunk[0].close()
                except (BufferError, OSError):
                    pass
                want -= size
                continue
            # Partial release: free the whole chunk, then re-allocate the rest.
            try:
                chunk[0].close()
            except (BufferError, OSError):
                pass
            rest = self._alloc(size - want)
            if rest is not None:
                self.chunks.append(rest)
            want = 0
        return self.held_mb

    def set_mb(self, target_mb):
        target = max(0.0, float(target_mb))
        delta = target - self.held_mb
        if delta > 0.01:
            return self.grow(delta)
        if delta < -0.01:
            return self.shrink(-delta)
        return self.held_mb

    def release_all(self):
        for chunk in self.chunks:
            try:
                chunk[0].close()
            except (BufferError, OSError):
                pass
        self.chunks = []
        return self.held_mb


# --------------------------------------------------------------------------
# CPU burn
# --------------------------------------------------------------------------

def _set_idle_scheduler():
    """Best effort SCHED_IDLE so real workloads always win.  Never fatal."""
    try:
        os.sched_setscheduler(0, os.SCHED_IDLE, os.sched_param(0))
    except (AttributeError, OSError, ValueError):
        pass


def burn_for(seconds):
    """Tight integer loop; checks the clock roughly every 10k iterations."""
    end = time.monotonic() + float(seconds)
    x = 1
    iters = 0
    while True:
        for _ in range(10000):
            x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        iters += 10000
        if time.monotonic() >= end:
            break
    return x, iters


def _burn_worker(seconds):
    _set_idle_scheduler()
    burn_for(seconds)


def _mp_context():
    try:
        return multiprocessing.get_context("fork")
    except (AttributeError, ValueError):
        return multiprocessing.get_context()


class BurstRunner(object):
    """Owns the CPU worker processes for one burst at a time."""

    def __init__(self, workers, burst_seconds, ctx=None):
        self.workers = max(1, int(workers))
        self.burst = float(burst_seconds)
        self.ctx = ctx if ctx is not None else _mp_context()
        self.procs = []
        self.started_at = 0.0

    @property
    def active(self):
        return any(p.is_alive() for p in self.procs)

    def start(self):
        self.procs = []
        for _ in range(self.workers):
            p = self.ctx.Process(target=_burn_worker, args=(self.burst,), daemon=True)
            p.start()
            self.procs.append(p)
        self.started_at = time.monotonic()
        return self.workers

    def stop(self):
        for p in self.procs:
            if p.is_alive():
                p.terminate()
        deadline = time.monotonic() + 2.0
        for p in self.procs:
            p.join(max(0.1, deadline - time.monotonic()))
            if p.is_alive():
                try:
                    p.kill()
                except AttributeError:  # pragma: no cover - py3.6 and older
                    pass
                p.join(1.0)
        self.procs = []

    def reap(self):
        """Drop worker objects that died on their own (e.g. burst finished)."""
        self.procs = [p for p in self.procs if p.is_alive()]


# --------------------------------------------------------------------------
# state directory helpers
# --------------------------------------------------------------------------

def _pid_path(state_dir):
    return os.path.join(state_dir, "daemon.pid")


def _heartbeat_path(state_dir):
    return os.path.join(state_dir, "heartbeat")


def write_pidfile(state_dir, pid):
    path = _pid_path(state_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("%d\n" % int(pid))
    os.replace(tmp, path)


def read_pidfile(state_dir):
    try:
        with open(_pid_path(state_dir), "r", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def remove_pidfile(state_dir):
    for path in (_pid_path(state_dir), _heartbeat_path(state_dir)):
        try:
            os.unlink(path)
        except OSError:
            pass


def touch_heartbeat(state_dir):
    path = _heartbeat_path(state_dir)
    try:
        with open(path, "a", encoding="utf-8"):
            pass
        now = time.time()
        os.utime(path, (now, now))
        return True
    except OSError:
        return False


def heartbeat_age(state_dir):
    try:
        return max(0.0, time.time() - os.path.getmtime(_heartbeat_path(state_dir)))
    except OSError:
        return None


def pid_alive(pid):
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # A recycled pid is caught by the heartbeat check in service_state(), so
    # liveness alone is enough here.
    return True


def service_state(state_dir, stale_after=30.0):
    """(state_string, pid, heartbeat_age_seconds) without ever calling systemctl."""
    pid = read_pidfile(state_dir)
    age = heartbeat_age(state_dir)
    if pid is None:
        return "not running (no pidfile)", None, age
    if not pid_alive(pid):
        return "not running (stale pidfile: pid %d is gone)" % pid, pid, age
    if age is None:
        return "running (pid %d, no heartbeat yet)" % pid, pid, age
    if age > stale_after:
        return "not running (pid %d alive but heartbeat is %.0fs old)" % (pid, age), pid, age
    return "running (pid %d, heartbeat %.0fs ago)" % (pid, age), pid, age


def human_gib(kb):
    return "%.1f GiB" % (float(kb) / 1024.0 / 1024.0)


def pid_cmdline(pid, proc_root=None):
    """Text of /proc/<pid>/cmdline, or None when it cannot be read."""
    root = PROC_ROOT if proc_root is None else proc_root
    try:
        with open(os.path.join(root, str(int(pid)), "cmdline"), "rb") as fh:
            raw = fh.read()
    except (OSError, ValueError):
        return None
    return raw.decode("utf-8", "replace")


def pid_is_ours(pid, proc_root=None):
    """True only for *another* live process whose cmdline is this program.

    After a SIGKILL or an OOM kill the pidfile and a recent heartbeat both
    survive, and in the meantime the pid may even have been recycled.  The
    heartbeat alone therefore cannot be trusted at startup; /proc/<pid>/cmdline
    is cheap proof that the pid still belongs to oracle-keepalive.  An
    unreadable or non-matching cmdline means the pidfile is stale.
    """
    if pid is None or pid <= 0:
        return False
    if pid == os.getpid() or pid == os.getppid():
        return False
    if not pid_alive(pid):
        return False
    cmdline = pid_cmdline(pid, proc_root)
    if not cmdline:
        return False
    return PROG in cmdline


def fmt_ts(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


# --------------------------------------------------------------------------
# daemon
# --------------------------------------------------------------------------

class Daemon(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.state_dir = cfg.state_dir
        self.samples_path = os.path.join(cfg.state_dir, SAMPLE_FILE)
        self.stop = False
        self.stop_signal = None
        self.hold = MemoryHold()
        self.runner = None
        self.sampler_prev = None
        self.burning = False

    # -- signals ---------------------------------------------------------
    def _on_signal(self, signum, frame):
        self.stop = True
        self.stop_signal = signum

    # -- memory ----------------------------------------------------------
    def memory_tick(self):
        mi = read_meminfo()
        total = int(mi.get("MemTotal", 0) or 0)
        if total <= 0:
            return
        avail = available_kb(mi)
        before = self.hold.held_mb
        want = desired_hold_mb(total, avail, self.hold.held_kb,
                               self.cfg.mem_target, self.cfg.mem_max_hold, self.cfg.mem_floor)
        target = apply_hysteresis(before, want, self.cfg.mem_step_mb)
        if abs(target - before) < 0.5:
            return
        after = self.hold.set_mb(target)
        if abs(after - before) >= 0.5:
            log("memory hold %.0f MB -> %.0f MB (desired %.0f MB, MemAvailable %s, floor %.0f%%)"
                % (before, after, want, human_gib(avail), self.cfg.mem_floor * 100))

    # -- sampling --------------------------------------------------------
    def take_sample(self):
        try:
            cur = read_proc_stat()
        except (OSError, ValueError):
            return None
        if self.sampler_prev is None:
            self.sampler_prev = cur
            return None
        cpu_pct, steal_pct = cpu_delta(self.sampler_prev, cur)
        self.sampler_prev = cur
        mi = read_meminfo()
        mem_pct = mem_util_pct(mi)
        held_mb = self.hold.held_mb if self.hold is not None else 0.0
        epoch = time.time()
        try:
            append_sample(self.samples_path, epoch, cpu_pct, mem_pct, steal_pct, held_mb, self.burning)
        except OSError as exc:
            warn("cannot append sample: %s" % exc)
            return None
        return (int(epoch), cpu_pct, mem_pct, steal_pct)

    # -- main loop -------------------------------------------------------
    def run(self):
        cfg = self.cfg
        problem = state_dir_problem(self.state_dir)
        if problem:
            log("ERROR: %s; refusing to start until the state dir is fixed" % problem)
            return 1
        try:
            os.makedirs(self.state_dir, exist_ok=True)
        except OSError as exc:
            log("ERROR: cannot create state dir %s: %s" % (self.state_dir, exc))
            return 1

        if not cfg.cpu_enabled and not cfg.mem_enabled:
            log("nothing to do (mode=%s); exiting" % cfg.mode)
            return 0

        stale_after = max(30.0, 3.0 * cfg.tick)
        state, pid, _age = service_state(self.state_dir, stale_after)
        if state.startswith("running"):
            # A fresh heartbeat plus a live pid is not proof on its own: after a
            # SIGKILL/OOM kill both survive and systemd restarts us 30 s later.
            if pid_is_ours(pid):
                log("ERROR: another instance is already running (%s)" % state)
                return 1
            warn("pidfile says %r but pid %s is not a live %s process; "
                 "treating the pidfile as stale and continuing" % (state, pid, PROG))

        for note in cfg.notes:
            warn(note)
        log("starting: %s" % cfg.summary())
        log("machine=%s ncpu=%d MemTotal=%s state_dir=%s"
            % (cfg.machine, cfg.ncpu, human_gib(cfg.memtotal_kb), self.state_dir))

        write_pidfile(self.state_dir, os.getpid())
        touch_heartbeat(self.state_dir)

        removed = trim_samples(self.samples_path)
        if removed:
            log("samples: trimmed %d row(s) older than %d days" % (removed, SAMPLE_RETENTION_DAYS))

        self.hold = MemoryHold()
        try:
            self.sampler_prev = read_proc_stat()
        except (OSError, ValueError):
            self.sampler_prev = None

        sched = BurstSchedule(cfg.cpu_burst, cfg.cpu_duty)
        self.runner = BurstRunner(cfg.cpu_workers, cfg.cpu_burst)
        history = []
        next_burst = time.monotonic()
        burst_end = 0.0
        next_tick = time.monotonic()
        next_sample = time.monotonic() + cfg.sample
        next_trim = time.monotonic() + 3600.0
        next_summary = time.monotonic() + 3600.0

        try:
            while not self.stop:
                now = time.monotonic()

                # CPU burst state machine
                if cfg.cpu_enabled:
                    if not self.runner.active and now >= next_burst:
                        self.burning = True
                        self.runner.start()
                        burst_end = time.monotonic() + cfg.cpu_burst
                        log("cpu burst started: %d worker(s), %.0fs (duty %.0f%%, period %.0fs)"
                            % (cfg.cpu_workers, cfg.cpu_burst, cfg.cpu_duty * 100, cfg.period_seconds))
                    elif self.runner.active and now >= burst_end:
                        self.runner.stop()
                        self.burning = False
                        gap = sched.next_idle_gap()
                        next_burst = time.monotonic() + gap
                        log("cpu burst finished: next burst in %.0fs" % gap)

                if cfg.mem_enabled and now >= next_tick:
                    self.memory_tick()
                    next_tick = now + cfg.tick

                if now >= next_sample:
                    row = self.take_sample()
                    if row is not None:
                        history.append(row)
                        if len(history) > 20000:
                            del history[:5000]
                    next_sample = now + cfg.sample

                if now >= next_trim:
                    trim_samples(self.samples_path)
                    next_trim = now + 3600.0

                if now >= next_summary:
                    self.log_summary(history)
                    next_summary = now + 3600.0

                touch_heartbeat(self.state_dir)
                time.sleep(0.5)
        finally:
            self.burning = False
            try:
                self.runner.stop()
            except Exception:  # pragma: no cover - shutdown must not raise
                pass
            if self.hold is not None:
                freed = self.hold.held_mb
                self.hold.release_all()
                if freed > 0:
                    log("released %.0f MB of held memory" % freed)
            remove_pidfile(self.state_dir)

        if self.stop_signal:
            log("stopped by signal %d" % self.stop_signal)
        else:
            log("stopped")
        return 0

    def log_summary(self, history):
        cutoff = time.time() - 3600.0
        recent = [h for h in history if h[0] >= cutoff]
        if not recent:
            return
        log("summary: last 1h guest-view p95 cpu=%.1f%% mem=%.1f%% steal=%.2f%% held=%.0f MB"
            % (p95([r[1] for r in recent]), p95([r[2] for r in recent]),
               p95([r[3] for r in recent]),
               self.hold.held_mb if self.hold is not None else 0.0))


# --------------------------------------------------------------------------
# plan / status / selftest
# --------------------------------------------------------------------------

def print_config_notes(cfg):
    for note in cfg.notes:
        print("  note            : %s" % note)


def swap_note(meminfo):
    """Return (warning_text_or_None, swap_gib)."""
    swap_kb = int(meminfo.get("SwapTotal", 0) or 0)
    if swap_kb <= 0:
        return None, 0.0
    return ("swap is present (%s): held pages may be swapped out under pressure, "
            "which lowers guest-visible memory utilization and risks I/O stalls"
            % human_gib(swap_kb)), swap_kb / 1024.0 / 1024.0


def cmd_plan(cfg):
    """Print the detection result and the numbers.  No side effects."""
    mi = read_meminfo()
    total = int(mi.get("MemTotal", 0) or 0)
    avail = available_kb(mi)
    print("%s %s plan (read-only, nothing is enabled or written)" % (PROG, VERSION))
    print("  machine         : %s (%d cpu, %s)" % (cfg.machine, cfg.ncpu, human_gib(total)))
    print("  mode requested  : %s" % cfg.mode)
    print("  mode effective  : cpu=%s mem=%s" % (cfg.cpu_enabled, cfg.mem_enabled))
    print_config_notes(cfg)
    print("  memory now      : total %s, available %s, used %.1f%%"
          % (human_gib(total), human_gib(avail), mem_util_pct(mi)))
    if cfg.mem_enabled and total > 0:
        want = desired_hold_mb(total, avail, 0, cfg.mem_target, cfg.mem_max_hold, cfg.mem_floor)
        target = apply_hysteresis(0.0, want, cfg.mem_step_mb)
        print("  memory hold     : desired %.0f MB (%.1f%% of RAM), first tick %.0f MB, "
              "cap %.0f MB, floor %.0f%%"
              % (want, 100.0 * want * 1024.0 / total, target,
                 cfg.mem_max_hold * total / 1024.0, cfg.mem_floor * 100))
        print("  memory estimate : used would become about %.1f%% after ramp-up"
              % (100.0 * (total - avail + min(want, cfg.mem_max_hold * total) * 1024.0) / total))
    else:
        print("  memory hold     : disabled (memory is not part of the idle test on this shape)")
    if cfg.cpu_enabled:
        print("  cpu plan        : %d worker(s), %.0fs burst every %.0fs (duty %.0f%%), "
              "SCHED_IDLE, phase offset random"
              % (cfg.cpu_workers, cfg.cpu_burst, cfg.period_seconds, cfg.cpu_duty * 100))
        burst_pct = min(100.0, 100.0 * cfg.cpu_workers / float(cfg.ncpu))
        print("  cpu estimate    : a burst shows about %.0f%% of the whole box (%d worker(s) of %d cpu); "
              "p95 over 7 days should track that number, because %.0f%% of wall time is bursting "
              "and the 95th percentile only needs more than 5%% of samples up there"
              % (burst_pct, cfg.cpu_workers, cfg.ncpu, cfg.cpu_duty * 100))
    else:
        print("  cpu plan        : disabled")
    print("  state dir       : %s (samples.csv, daemon.pid, heartbeat)" % cfg.state_dir)
    text, _gib = swap_note(mi)
    if text:
        print("  warning         : %s" % text)
    print("  note            : all numbers are guest-view ESTIMATES; the OCI console "
          "Monitoring metrics are authoritative.")


def _status_samples(samples_path):
    rows = read_samples(samples_path)
    return rows


def cmd_status(cfg):
    mi = read_meminfo()
    rows = _status_samples(os.path.join(cfg.state_dir, SAMPLE_FILE))
    stale_after = max(30.0, 3.0 * cfg.tick)
    state, pid, age = service_state(cfg.state_dir, stale_after)

    print("%s %s status" % (PROG, VERSION))
    print("  mode            : %s (cpu=%s mem=%s, machine %s)"
          % (cfg.mode, cfg.cpu_enabled, cfg.mem_enabled, cfg.machine))
    if cfg.cpu_enabled:
        print("  cpu             : duty %.0f%%, burst %.0fs, period %.0fs, %d worker(s)"
              % (cfg.cpu_duty * 100, cfg.cpu_burst, cfg.period_seconds, cfg.cpu_workers))
    else:
        print("  cpu             : disabled")
    if cfg.mem_enabled:
        print("  memory          : target %.0f%%, cap %.0f%%, floor %.0f%%, step %.0f MB, "
              "MemTotal %s" % (cfg.mem_target * 100, cfg.mem_max_hold * 100,
                               cfg.mem_floor * 100, cfg.mem_step_mb, human_gib(cfg.memtotal_kb)))
    else:
        print("  memory          : disabled")
    print("  state dir       : %s" % cfg.state_dir)
    print("  service         : %s" % state)
    print_config_notes(cfg)

    if not rows:
        print("  samples         : none yet (%s)" % os.path.join(cfg.state_dir, SAMPLE_FILE))
    else:
        first = min(r["epoch"] for r in rows)
        last = max(r["epoch"] for r in rows)
        hours = (last - first) / 3600.0
        print("  samples         : %d rows, %.1f h covered (%s .. %s)"
              % (len(rows), hours, fmt_ts(first), fmt_ts(last)))
        if hours < 24 * 7:
            print("  sample coverage : less than a full 7-day window (%.1f h of 168 h)" % hours)
        cpu_p95 = p95([r["cpu"] for r in rows])
        mem_p95 = p95([r["mem"] for r in rows])
        mem_mean = mean([r["mem"] for r in rows])
        steal_p95 = p95([r["steal"] for r in rows])
        held = rows[-1]["held_mb"]
        print("  guest p95 (7d)  : cpu %.1f%%  mem %.1f%%  steal %.2f%%"
              % (cpu_p95, mem_p95, steal_p95))
        print("  memory util     : mean %.1f%%, p95 %.1f%%" % (mem_mean, mem_p95))
        print("  current hold    : %.0f MB (%.1f%% of %s)"
              % (held, 100.0 * held / (cfg.memtotal_kb / 1024.0) if cfg.memtotal_kb else 0.0,
                 human_gib(cfg.memtotal_kb)))
        print("  verdict         : guest-view ESTIMATE - %s"
              % _verdict_text(cpu_p95, mem_p95, cfg))

    text, _gib = swap_note(mi)
    if text:
        print("  warning         : %s" % text)
    print("  NOTE            : guest-view ESTIMATE from /proc only. The OCI console "
          "Monitoring metrics (CpuUtilization, MemoryUtilization) are authoritative.")
    return 0


def _verdict_text(cpu_p95, mem_p95, cfg):
    broken = []
    if cpu_p95 >= IDLE_LINE_PCT:
        broken.append("cpu p95 %.1f%% >= %.0f%%" % (cpu_p95, IDLE_LINE_PCT))
    if cfg.mem_enabled and mem_p95 >= IDLE_LINE_PCT:
        broken.append("memory p95 %.1f%% >= %.0f%%" % (mem_p95, IDLE_LINE_PCT))
    if broken:
        return ("%s -> the idle condition is not met on that metric (any one is enough). "
                "Network is not measured here." % ", ".join(broken))
    return ("all measured p95 values are below %.0f%%; if Oracle measures the same way, only "
            "the network condition could keep the instance out of the idle bucket. Network is "
            "not measured here - check the console before trusting this." % IDLE_LINE_PCT)


def cmd_selftest(cfg):
    """About 5 s: parse the real /proc, burn 2 s in one worker, hold 32 MB and release."""
    failures = []
    mi = {}
    t0 = time.time()
    print("%s %s selftest" % (PROG, VERSION))

    try:
        mi = read_meminfo()
        total = int(mi.get("MemTotal", 0) or 0)
        if total <= 0:
            failures.append("/proc/meminfo: MemTotal missing")
        else:
            print("  ok   /proc/meminfo: %s total, %.1f%% used"
                  % (human_gib(total), mem_util_pct(mi)))
    except Exception as exc:  # pragma: no cover - defensive
        failures.append("/proc/meminfo: %s" % exc)

    try:
        snap = read_proc_stat()
        if snap["total"] <= 0:
            failures.append("/proc/stat: aggregate cpu total is zero")
        else:
            print("  ok   /proc/stat: %d ticks total, %d idle" % (snap["total"], snap["idle_total"]))
    except Exception as exc:
        failures.append("/proc/stat: %s" % exc)

    # CPU worker: one process, ~2 s, must exit cleanly.
    try:
        started = time.monotonic()
        proc = _mp_context().Process(target=_burn_worker, args=(2.0,), daemon=True)
        proc.start()
        proc.join(8.0)
        elapsed = time.monotonic() - started
        if proc.is_alive():
            proc.terminate()
            proc.join(1.0)
            failures.append("cpu worker did not finish within 8 s")
        elif proc.exitcode != 0:
            failures.append("cpu worker exited with %s" % proc.exitcode)
        elif elapsed < 1.5:
            failures.append("cpu worker returned after only %.2fs" % elapsed)
        else:
            print("  ok   cpu worker: 1 process, %.1fs, exit 0 (SCHED_IDLE requested)" % elapsed)
    except Exception as exc:
        failures.append("cpu worker: %s" % exc)

    # Memory hold: 32 MB, touched, then released.
    hold = MemoryHold()
    try:
        hold.grow(32)
        held = hold.held_mb
        if abs(held - 32.0) > 1.0:
            failures.append("memory hold reported %.1f MB, expected 32" % held)
        else:
            print("  ok   memory hold: %.0f MB allocated and touched (anonymous mmap)" % held)
    except Exception as exc:
        failures.append("memory hold: %s" % exc)
    finally:
        hold.release_all()
    if hold.held_mb > 0.5:
        failures.append("memory hold did not release (%.1f MB left)" % hold.held_mb)
    else:
        print("  ok   memory hold: released")

    # Config sanity on this host.
    try:
        cfg2 = load_config(env=os.environ, meminfo=mi)
        if not (DUTY_MIN <= cfg2.cpu_duty <= DUTY_MAX):
            failures.append("cpu duty %.3f outside clamps" % cfg2.cpu_duty)
        print("  ok   config: mode=%s cpu=%s mem=%s workers=%d duty=%.2f"
              % (cfg2.mode, cfg2.cpu_enabled, cfg2.mem_enabled, cfg2.cpu_workers, cfg2.cpu_duty))
    except Exception as exc:
        failures.append("config: %s" % exc)

    took = time.time() - t0
    if failures:
        for f in failures:
            print("  FAIL %s" % f)
        print("selftest: FAILED (%d problem(s), %.1fs)" % (len(failures), took))
        return 1
    print("selftest: OK (%.1fs)" % took)
    return 0


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Keep Oracle Cloud Always Free instances from being classified idle.")
    parser.add_argument("--version", action="version", version="%s %s" % (PROG, VERSION))
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="run the daemon (systemd ExecStart)")
    sub.add_parser("status", help="show mode, samples and a guest-view p95 verdict")
    sub.add_parser("plan", help="print detection and sizing, without side effects")
    sub.add_parser("selftest", help="about 5 s of local checks; exit 0 on success")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "status"

    if command == "run":
        cfg = load_config()
        daemon = Daemon(cfg)
        def _handler(signum, frame):
            daemon._on_signal(signum, frame)
        try:
            signal.signal(signal.SIGTERM, _handler)
            signal.signal(signal.SIGINT, _handler)
        except ValueError:  # not the main thread (e.g. under a test harness)
            pass
        return daemon.run()

    cfg = load_config()
    if command == "plan":
        cmd_plan(cfg)
        return 0
    if command == "status":
        return cmd_status(cfg)
    if command == "selftest":
        return cmd_selftest(cfg)
    parser.error("unknown command %r" % command)
    return 2


if __name__ == "__main__":
    sys.exit(main())
