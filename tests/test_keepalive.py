"""Unit tests for oracle-keepalive.

Run with either of:
    python3 -m unittest discover -s tests -v
    python3 -m pytest tests -q

Standard library only.  Nothing here touches /var/lib, systemd or the network;
the one long-ish test is `selftest` (about 3-4 s).
"""

import contextlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = ROOT / "oracle-keepalive.py"
INSTALLER = ROOT / "install.sh"


def _load_program():
    spec = importlib.util.spec_from_file_location("oracle_keepalive", str(PROGRAM))
    module = importlib.util.module_from_spec(spec)
    sys.modules["oracle_keepalive"] = module
    spec.loader.exec_module(module)
    return module


ok = _load_program()

MEMINFO_SAMPLE = """\
MemTotal:       12582912 kB
MemFree:         1180000 kB
MemAvailable:   12000000 kB
Buffers:          120000 kB
Cached:          5400000 kB
SwapCached:            0 kB
SwapTotal:       2097152 kB
SwapFree:        2097152 kB
"""

PROC_STAT_SAMPLE = """\
cpu  1000 20 300 8000 100 5 5 40 0 0
cpu0 500 10 150 4000 50 2 3 20 0 0
cpu1 500 10 150 4000 50 3 2 20 0 0
intr 12345
ctxt 999
"""


class MeminfoTests(unittest.TestCase):
    def test_parses_kb_values(self):
        info = ok.parse_meminfo(MEMINFO_SAMPLE)
        self.assertEqual(info["MemTotal"], 12582912)
        self.assertEqual(info["MemAvailable"], 12000000)
        self.assertEqual(info["SwapTotal"], 2097152)

    def test_skips_junk_without_dying(self):
        info = ok.parse_meminfo("MemTotal: 100 kB\nnonsense line\nBad:\nZero: ab kB\n")
        self.assertEqual(info["MemTotal"], 100)
        self.assertNotIn("Bad", info)
        self.assertNotIn("Zero", info)

    def test_live_meminfo_is_readable(self):
        info = ok.read_meminfo()
        self.assertGreater(info.get("MemTotal", 0), 0)

    def test_mem_util_from_available(self):
        info = ok.parse_meminfo(MEMINFO_SAMPLE)
        self.assertAlmostEqual(ok.mem_util_pct(info), 100.0 * (12582912 - 12000000) / 12582912, places=6)

    def test_mem_util_falls_back_without_memavailable(self):
        info = {"MemTotal": 1000, "MemFree": 100, "Buffers": 100, "Cached": 300}
        self.assertAlmostEqual(ok.mem_util_pct(info), 50.0, places=6)

    def test_mem_util_with_no_total(self):
        self.assertEqual(ok.mem_util_pct({}), 0.0)


class ProcStatTests(unittest.TestCase):
    def test_aggregate_line_parsed(self):
        ticks = ok.parse_proc_stat(PROC_STAT_SAMPLE)
        self.assertEqual(ticks["user"], 1000)
        self.assertEqual(ticks["steal"], 40)
        self.assertEqual(ticks["idle_total"], 8100)
        self.assertEqual(ticks["total"], 1000 + 20 + 300 + 8000 + 100 + 5 + 5 + 40)

    def test_guest_not_double_counted(self):
        ticks = ok.parse_proc_stat("cpu  100 0 100 700 0 0 0 0 50 0\n")
        self.assertEqual(ticks["total"], 900)

    def test_missing_line_raises(self):
        with self.assertRaises(ValueError):
            ok.parse_proc_stat("intr 1\nctxt 2\n")

    def test_cpu_delta_math(self):
        prev = ok.parse_proc_stat("cpu  100 0 100 800 0 0 0 0 0 0\n")   # total 1000
        cur = ok.parse_proc_stat("cpu  150 0 150 850 0 0 0 5 0 0\n")    # total 1155
        cpu, steal = ok.cpu_delta(prev, cur)
        self.assertAlmostEqual(cpu, 100.0 * (155 - 50) / 155, places=6)  # idle grew by 50 of 155
        self.assertAlmostEqual(steal, 100.0 * 5 / 155, places=6)

    def test_cpu_delta_degenerate(self):
        snap = ok.parse_proc_stat(PROC_STAT_SAMPLE)
        self.assertEqual(ok.cpu_delta(snap, snap), (0.0, 0.0))

    def test_cpu_delta_clamped(self):
        prev = ok.parse_proc_stat("cpu  0 0 0 1000 0 0 0 0 0 0\n")
        cur = ok.parse_proc_stat("cpu  10 0 0 0 0 0 0 0 0 0\n")  # counters reset
        cpu, steal = ok.cpu_delta(prev, cur)
        self.assertGreaterEqual(cpu, 0.0)
        self.assertLessEqual(cpu, 100.0)
        self.assertGreaterEqual(steal, 0.0)

    def test_live_proc_stat_is_readable(self):
        self.assertGreater(ok.read_proc_stat()["total"], 0)


class DesiredHoldTests(unittest.TestCase):
    TOTAL_12G = 12 * 1024 * 1024  # kB

    def test_idle_12g_host(self):
        total = self.TOTAL_12G
        got = ok.desired_hold_mb(total, 12000000, 0, 0.25, 0.30, 0.10)
        expected_kb = 0.25 * total - (total - 12000000)
        self.assertAlmostEqual(got, expected_kb / 1024.0, delta=1.0)
        self.assertGreater(got, 2000.0)  # ~2.4 GiB on an otherwise idle box

    def test_busy_host_already_above_target(self):
        total = self.TOTAL_12G
        self.assertEqual(ok.desired_hold_mb(total, 6000000, 0, 0.25, 0.30, 0.10), 0.0)

    def test_below_floor_releases_everything(self):
        total = self.TOTAL_12G
        avail = int(total * 0.09)
        self.assertEqual(ok.desired_hold_mb(total, avail, 1024 * 1024, 0.25, 0.30, 0.10), 0.0)

    def test_cap_is_respected(self):
        total = self.TOTAL_12G
        got = ok.desired_hold_mb(total, total, 0, 0.40, 0.30, 0.10)
        self.assertAlmostEqual(got, 0.30 * total / 1024.0, delta=1.0)

    def test_held_memory_does_not_feed_back(self):
        total = self.TOTAL_12G
        held_kb = 1024 * 1024
        avail = int(total * 0.75)  # used == target*total, so our hold is exactly right
        got = ok.desired_hold_mb(total, avail, held_kb, 0.25, 0.30, 0.10)
        self.assertAlmostEqual(got, held_kb / 1024.0, delta=1.0)

    def test_zero_total_is_safe(self):
        self.assertEqual(ok.desired_hold_mb(0, 0, 0, 0.25, 0.30, 0.10), 0.0)

    def test_avail_larger_than_total_is_clamped(self):
        # avail is clamped to total (used=0), so the answer is simply target*total.
        self.assertAlmostEqual(ok.desired_hold_mb(1000, 2000, 0, 0.25, 0.30, 0.10),
                               250.0 / 1024.0, places=6)


class HysteresisTests(unittest.TestCase):
    def test_deadband_keeps_current(self):
        self.assertEqual(ok.apply_hysteresis(1000.0, 1040.0, 64), 1000.0)
        self.assertEqual(ok.apply_hysteresis(1000.0, 960.0, 64), 1000.0)

    def test_growth_is_limited_to_256_mb_per_tick(self):
        self.assertEqual(ok.apply_hysteresis(0.0, 2502.0, 64), 256.0)
        self.assertEqual(ok.apply_hysteresis(2048.0, 2502.0, 64), 2304.0)

    def test_shrink_is_immediate(self):
        self.assertEqual(ok.apply_hysteresis(2500.0, 1200.0, 64), 1200.0)

    def test_zero_desired_always_releases(self):
        self.assertEqual(ok.apply_hysteresis(30.0, 0.0, 64), 0.0)
        self.assertEqual(ok.apply_hysteresis(4096.0, -1.0, 64), 0.0)


class ModeDetectionTests(unittest.TestCase):
    def test_aarch64_auto_gets_cpu_and_mem(self):
        cpu, mem, notes = ok.resolve_mode("auto", "aarch64", 12 * 1024 * 1024)
        self.assertTrue(cpu)
        self.assertTrue(mem)
        self.assertTrue(any("auto" in n for n in notes))

    def test_x86_64_auto_gets_cpu_only(self):
        cpu, mem, _ = ok.resolve_mode("auto", "x86_64", 1024 * 1024)
        self.assertTrue(cpu)
        self.assertFalse(mem)

    def test_small_ram_disables_forced_mem(self):
        cpu, mem, notes = ok.resolve_mode("cpu+mem", "aarch64", 1024 * 1024)
        self.assertTrue(cpu)
        self.assertFalse(mem)
        self.assertTrue(any("2 GiB" in n for n in notes))

    def test_forced_mem_on_big_arm_host(self):
        cpu, mem, _ = ok.resolve_mode("mem", "arm64", 24 * 1024 * 1024)
        self.assertFalse(cpu)
        self.assertTrue(mem)

    def test_off(self):
        self.assertEqual(ok.resolve_mode("off", "aarch64", 12 * 1024 * 1024)[:2], (False, False))

    def test_garbage_mode_falls_back_to_auto(self):
        cpu, mem, notes = ok.resolve_mode("turbo", "x86_64", 4 * 1024 * 1024)
        self.assertTrue(cpu)
        self.assertFalse(mem)
        self.assertTrue(any("not one of" in n for n in notes))

    def test_unknown_memtotal_keeps_mem(self):
        _cpu, mem, _ = ok.resolve_mode("cpu+mem", "aarch64", 0)
        self.assertTrue(mem)


class ConfigTests(unittest.TestCase):
    MEMINFO = ok.parse_meminfo(MEMINFO_SAMPLE)

    def make(self, **env):
        return ok.load_config(env=env, machine="aarch64", ncpu=4, meminfo=self.MEMINFO)

    def test_defaults(self):
        cfg = self.make()
        self.assertAlmostEqual(cfg.cpu_duty, 0.12)
        self.assertAlmostEqual(cfg.cpu_burst, 180)
        self.assertEqual(cfg.cpu_workers, 2)  # ceil(4/2)
        self.assertAlmostEqual(cfg.mem_target, 0.25)
        self.assertAlmostEqual(cfg.mem_max_hold, 0.30)
        self.assertAlmostEqual(cfg.mem_floor, 0.10)
        self.assertEqual(cfg.mem_step_mb, 64)
        self.assertEqual(cfg.tick, 5)
        self.assertEqual(cfg.sample, 60)
        self.assertEqual(cfg.state_dir, "/var/lib/oracle-keepalive")
        self.assertTrue(cfg.cpu_enabled and cfg.mem_enabled)
        self.assertAlmostEqual(cfg.period_seconds, 180 / 0.12)

    def test_duty_clamped_low(self):
        cfg = self.make(KEEPALIVE_CPU_DUTY="0.01")
        self.assertAlmostEqual(cfg.cpu_duty, 0.06)
        self.assertTrue(any("KEEPALIVE_CPU_DUTY" in n for n in cfg.notes))

    def test_duty_clamped_high(self):
        self.assertAlmostEqual(self.make(KEEPALIVE_CPU_DUTY="0.9").cpu_duty, 0.40)

    def test_burst_clamped(self):
        self.assertAlmostEqual(self.make(KEEPALIVE_CPU_BURST_SECONDS="10").cpu_burst, 60)
        self.assertAlmostEqual(self.make(KEEPALIVE_CPU_BURST_SECONDS="9999").cpu_burst, 600)

    def test_workers_clamped_to_ncpu(self):
        self.assertEqual(self.make(KEEPALIVE_CPU_WORKERS="99").cpu_workers, 4)
        self.assertEqual(self.make(KEEPALIVE_CPU_WORKERS="0").cpu_workers, 1)

    def test_workers_default_single_cpu(self):
        cfg = ok.load_config(env={}, machine="x86_64", ncpu=1, meminfo=self.MEMINFO)
        self.assertEqual(cfg.cpu_workers, 1)

    def test_target_above_cap_is_lowered(self):
        cfg = self.make(KEEPALIVE_MEM_TARGET="0.40", KEEPALIVE_MEM_MAX_HOLD="0.22")
        self.assertAlmostEqual(cfg.mem_target, 0.22)
        self.assertTrue(any("MAX_HOLD" in n for n in cfg.notes))

    def test_bad_number_uses_default(self):
        cfg = self.make(KEEPALIVE_TICK_SECONDS="soon")
        self.assertEqual(cfg.tick, 5)
        self.assertTrue(any("not a number" in n for n in cfg.notes))

    def test_nan_is_rejected(self):
        cfg = self.make(KEEPALIVE_CPU_DUTY="nan")
        self.assertAlmostEqual(cfg.cpu_duty, 0.12)

    def test_tick_and_sample_clamped(self):
        self.assertEqual(self.make(KEEPALIVE_TICK_SECONDS="0").tick, 1)
        self.assertEqual(self.make(KEEPALIVE_SAMPLE_SECONDS="1").sample, 10)

    def test_state_dir_override(self):
        cfg = self.make(KEEPALIVE_STATE_DIR="/tmp/x")
        self.assertEqual(cfg.state_dir, "/tmp/x")

    def test_small_ram_switches_mem_off_through_config(self):
        meminfo = {"MemTotal": 1024 * 1024, "MemAvailable": 900 * 1024}
        cfg = ok.load_config(env={"KEEPALIVE_MODE": "cpu+mem"}, machine="aarch64",
                             ncpu=1, meminfo=meminfo)
        self.assertFalse(cfg.mem_enabled)

    def test_off_mode_disables_everything(self):
        cfg = self.make(KEEPALIVE_MODE="off")
        self.assertFalse(cfg.cpu_enabled)
        self.assertFalse(cfg.mem_enabled)


class PercentileTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(ok.p95([]), 0.0)

    def test_single(self):
        self.assertEqual(ok.p95([7.5]), 7.5)

    def test_nearest_rank(self):
        self.assertEqual(ok.p95(list(range(1, 101))), 95.0)
        self.assertEqual(ok.p95([1, 2, 3, 4]), 4.0)

    def test_unsorted_input(self):
        self.assertEqual(ok.p95([9, 1, 5, 3, 7]), 9.0)

    def test_mean(self):
        self.assertAlmostEqual(ok.mean([1, 2, 3]), 2.0)
        self.assertEqual(ok.mean([]), 0.0)


class SampleFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ok-samples-")
        self.path = os.path.join(self.tmp, "samples.csv")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_header_only_on_create_and_roundtrip(self):
        ok.append_sample(self.path, 1700000000, 12.5, 26.25, 0.5, 3000.0, True)
        ok.append_sample(self.path, 1700000060, 11.0, 26.0, 0.0, 3000.0, False)
        with open(self.path, "r", encoding="utf-8") as fh:
            lines = fh.read().strip().splitlines()
        self.assertEqual(lines[0], ok.SAMPLE_HEADER)
        self.assertEqual(len(lines), 3)
        rows = ok.read_samples(self.path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["epoch"], 1700000000)
        self.assertAlmostEqual(rows[0]["cpu"], 12.5)
        self.assertEqual(rows[0]["burning"], 1)
        self.assertEqual(rows[1]["burning"], 0)

    def test_malformed_rows_skipped(self):
        ok.append_sample(self.path, 1700000000, 5.0, 20.0, 0.0, 0.0, False)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("garbage\n1,2,3\n\n")
        self.assertEqual(len(ok.read_samples(self.path)), 1)

    def test_missing_file_reads_empty(self):
        self.assertEqual(ok.read_samples(os.path.join(self.tmp, "nope.csv")), [])

    def test_trim_keeps_seven_days(self):
        now = 1800000000
        for i in range(20):
            ok.append_sample(self.path, now - i * 86400, 10.0, 25.0, 0.0, 1000.0, False)
        removed = ok.trim_samples(self.path, now=now)
        rows = ok.read_samples(self.path)
        self.assertEqual(removed, 12)  # ages 8..19 days (7 days old is kept)
        self.assertEqual(len(rows), 8)
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_trim_noop_when_all_recent(self):
        ok.append_sample(self.path, 1800000000, 10.0, 25.0, 0.0, 1000.0, False)
        before = os.path.getmtime(self.path)
        self.assertEqual(ok.trim_samples(self.path, now=1800000000), 0)
        self.assertEqual(os.path.getmtime(self.path), before)

    def test_trim_on_missing_file(self):
        self.assertEqual(ok.trim_samples(os.path.join(self.tmp, "nope.csv")), 0)


class BurstScheduleTests(unittest.TestCase):
    def test_period_math(self):
        sched = ok.BurstSchedule(180, 0.12)
        self.assertAlmostEqual(sched.period, 1500.0)
        self.assertAlmostEqual(sched.idle, 1320.0)

    def test_gap_stays_within_jitter_bounds(self):
        sched = ok.BurstSchedule(180, 0.12)
        for _ in range(500):
            gap = sched.next_idle_gap()
            self.assertGreaterEqual(gap, 0.8 * sched.idle - 1e-9)
            self.assertLessEqual(gap, 1.2 * sched.idle + 1e-9)

    def test_long_run_duty_within_25_percent(self):
        sched = ok.BurstSchedule(180, 0.12)
        horizon = 2 * 86400
        busy, elapsed = sched.simulate(horizon)
        self.assertAlmostEqual(elapsed, horizon, delta=1e-6)
        duty = busy / elapsed
        self.assertAlmostEqual(duty, 0.12, delta=0.03)  # +-25% of 0.12
        self.assertLess(abs(duty - 0.12) / 0.12, 0.25)

    def test_long_run_duty_at_clamp_edges(self):
        for duty in (0.06, 0.40):
            sched = ok.BurstSchedule(120, duty)
            busy, elapsed = sched.simulate(2 * 86400)
            self.assertLess(abs(busy / elapsed - duty) / duty, 0.25, "duty=%s" % duty)

    def test_short_idle_gap_is_still_positive(self):
        sched = ok.BurstSchedule(60, 0.9)  # period 66.7 s, idle 6.7 s
        self.assertAlmostEqual(sched.period, 60 / 0.9, places=6)
        self.assertGreater(sched.next_idle_gap(), 1.0)


class MemoryHoldTests(unittest.TestCase):
    def setUp(self):
        self.hold = ok.MemoryHold()

    def tearDown(self):
        self.hold.release_all()

    def test_grow_and_release(self):
        self.hold.grow(4)
        self.assertAlmostEqual(self.hold.held_mb, 4.0, delta=0.1)
        self.hold.release_all()
        self.assertEqual(self.hold.held_mb, 0.0)

    def test_set_mb_is_idempotent(self):
        self.hold.set_mb(2)
        self.hold.set_mb(2)
        self.assertAlmostEqual(self.hold.held_mb, 2.0, delta=0.1)

    def test_partial_shrink(self):
        self.hold.set_mb(6)
        self.hold.set_mb(2.5)
        self.assertAlmostEqual(self.hold.held_mb, 2.5, delta=0.1)
        self.hold.set_mb(0)
        self.assertEqual(self.hold.held_mb, 0.0)

    def test_held_kb_matches_bytes(self):
        self.hold.grow(1)
        self.assertAlmostEqual(self.hold.held_kb, self.hold.held_bytes / 1024.0)


class PlanAndStatusTests(unittest.TestCase):
    """plan/status must work on a temp state dir and must not create anything."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ok-status-")
        self.state = os.path.join(self.tmp, "state")
        self._env = dict(os.environ)
        os.environ["KEEPALIVE_STATE_DIR"] = self.state
        os.environ.pop("KEEPALIVE_MODE", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_plan_has_no_side_effects(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ok.main(["plan"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.state), "plan must not create the state dir")
        self.assertIn("mode effective", out)
        self.assertIn("ESTIMATE", out)

    def test_status_without_samples(self):
        os.makedirs(self.state)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ok.main(["status"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("samples", out)
        self.assertIn("authoritative", out)
        self.assertIn("not running", out)

    def test_status_with_synthetic_samples(self):
        os.makedirs(self.state)
        path = os.path.join(self.state, "samples.csv")
        now = int(time.time())
        for i in range(48 * 30):  # 48 samples/day, 30 days back
            ok.append_sample(path, now - i * 1800, 13.5 + (i % 3), 26.0, 0.1, 2800.0, i % 8 == 0)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ok.main(["status"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("guest p95", out)
        self.assertIn("verdict", out)
        self.assertIn("ESTIMATE", out)
        self.assertIn("authoritative", out)
        self.assertIn("Network is not measured here", out.split("verdict")[1][:400])

    def test_status_flags_running_from_pidfile_and_heartbeat(self):
        os.makedirs(self.state)
        ok.write_pidfile(self.state, os.getpid())
        ok.touch_heartbeat(self.state)
        state, pid, age = ok.service_state(self.state, stale_after=300)
        self.assertTrue(state.startswith("running"), state)
        self.assertEqual(pid, os.getpid())
        self.assertIsNotNone(age)

    def test_status_detects_stale_pidfile(self):
        os.makedirs(self.state)
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        ok.write_pidfile(self.state, proc.pid)
        state, _pid, _age = ok.service_state(self.state, stale_after=30)
        self.assertIn("stale", state)

    def test_status_detects_stale_heartbeat(self):
        os.makedirs(self.state)
        ok.write_pidfile(self.state, os.getpid())
        hb = os.path.join(self.state, "heartbeat")
        with open(hb, "w", encoding="utf-8"):
            pass
        old = time.time() - 600
        os.utime(hb, (old, old))
        state, _pid, _age = ok.service_state(self.state, stale_after=30)
        self.assertIn("heartbeat is", state)

    def test_plan_mentions_swap_when_present(self):
        # Only meaningful on a host with swap; the machine running this test has
        # swap configured, so assert the branch is reachable via swap_note().
        text, swap_gib = ok.swap_note({"SwapTotal": 2 * 1024 * 1024})
        self.assertIsNotNone(text)
        self.assertIn("swapped", text)
        self.assertAlmostEqual(swap_gib, 2.0)
        self.assertIsNone(ok.swap_note({"SwapTotal": 0})[0])


class FakeHold(object):
    """Stand-in for MemoryHold so the tick loop can be tested without RAM."""

    def __init__(self, held_mb=0.0):
        self._mb = float(held_mb)
        self.calls = []

    @property
    def held_mb(self):
        return self._mb

    @property
    def held_kb(self):
        return self._mb * 1024.0

    def set_mb(self, target):
        self.calls.append(float(target))
        self._mb = max(0.0, float(target))
        return self._mb

    def release_all(self):
        self._mb = 0.0
        return 0.0


class MemoryTickWiringTests(unittest.TestCase):
    """The caller must apply hysteresis, the growth limit and the floor."""

    TOTAL_KB = 12 * 1024 * 1024

    def setUp(self):
        self._read = ok.read_meminfo
        # fake_avail is what MemAvailable would be *without* our hold; the fake
        # /proc subtracts the hold, exactly like a real kernel would.
        self.fake_avail = int(self.TOTAL_KB * 0.95)
        self.hold = FakeHold()
        setattr(ok, "read_meminfo", lambda path="/proc/meminfo": {
            "MemTotal": self.TOTAL_KB,
            "MemAvailable": max(0, self.fake_avail - int(self.hold.held_kb)),
            "SwapTotal": 0,
        })
        meminfo = {"MemTotal": self.TOTAL_KB, "MemAvailable": self.fake_avail}
        self.cfg = ok.load_config(env={"KEEPALIVE_MODE": "cpu+mem", "KEEPALIVE_CPU_WORKERS": "1"},
                                  machine="aarch64", ncpu=2, meminfo=meminfo)
        self.daemon = ok.Daemon(self.cfg)
        self.daemon.hold = self.hold

    def tearDown(self):
        setattr(ok, "read_meminfo", self._read)

    def test_growth_is_ramped_and_then_stops(self):
        for _ in range(40):
            self.daemon.memory_tick()
        # first tick limited to +256 MB
        self.assertAlmostEqual(self.hold.calls[0], 256.0, delta=0.5)
        # and it converges on the desired value, not the cap
        desired = ok.desired_hold_mb(self.TOTAL_KB, self.fake_avail, 0, self.cfg.mem_target,
                                     self.cfg.mem_max_hold, self.cfg.mem_floor)
        self.assertAlmostEqual(self.hold.held_mb, desired, delta=self.cfg.mem_step_mb)
        self.assertLessEqual(self.hold.held_mb, self.cfg.mem_max_hold * self.TOTAL_KB / 1024.0 + 1)

    def test_below_floor_releases_everything(self):
        self.daemon.memory_tick()
        self.assertGreater(self.hold.held_mb, 0.0)
        self.fake_avail = int(self.TOTAL_KB * 0.05)  # below the 10% floor
        self.daemon.memory_tick()
        self.assertEqual(self.hold.held_mb, 0.0)

    def test_small_delta_is_ignored(self):
        self.daemon.memory_tick()          # grows to 256 MB
        self.assertAlmostEqual(self.hold.held_mb, 256.0, delta=0.5)
        calls = len(self.hold.calls)
        # Make the desired hold land 44 MB above the current one: inside the
        # 64 MB dead band, so nothing must change.
        want_kb = 300 * 1024
        held_kb = self.hold.held_kb
        other_kb = self.cfg.mem_target * self.TOTAL_KB - want_kb
        self.fake_avail = int(self.TOTAL_KB - other_kb)
        avail_kb = self.fake_avail - held_kb
        desired = ok.desired_hold_mb(self.TOTAL_KB, avail_kb, held_kb,
                                     self.cfg.mem_target, self.cfg.mem_max_hold, self.cfg.mem_floor)
        self.assertAlmostEqual(desired, 300.0, delta=1.0)
        self.daemon.memory_tick()
        self.assertEqual(len(self.hold.calls), calls)


class SelfTestCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ok-selftest-")
        self.state = os.path.join(self.tmp, "state")
        self._env = dict(os.environ)
        os.environ["KEEPALIVE_STATE_DIR"] = self.state
        os.environ["KEEPALIVE_CPU_WORKERS"] = "1"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_selftest_passes_and_is_quick(self):
        buf = io.StringIO()
        started = time.time()
        with contextlib.redirect_stdout(buf):
            rc = ok.main(["selftest"])
        took = time.time() - started
        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        self.assertIn("selftest: OK", out)
        self.assertLess(took, 20.0)
        self.assertFalse(os.path.exists(self.state))


class RepoFileTests(unittest.TestCase):
    def test_bash_syntax_of_installer(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash not available")
        proc = subprocess.run([bash, "-n", str(ROOT / "install.sh")],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())

    def test_every_required_file_exists(self):
        for name in ("oracle-keepalive.py", "oracle-keepalive.service", "install.sh",
                     "oracle-keepalive.conf.example", "README.md", ".gitignore",
                     "tests/test_keepalive.py"):
            self.assertTrue((ROOT / name).is_file(), name)

    def test_unit_matches_the_documented_contract(self):
        text = (ROOT / "oracle-keepalive.service").read_text(encoding="utf-8")
        for needle in ("Type=simple", "Restart=on-failure", "RestartSec=30",
                       "Nice=19", "CPUSchedulingPolicy=idle", "IOSchedulingClass=idle",
                       "OOMScoreAdjust=1000", "DynamicUser=yes",
                       "StateDirectory=oracle-keepalive", "NoNewPrivileges=yes",
                       "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes",
                       "PrivateDevices=yes", "RestrictAddressFamilies=AF_UNIX",
                       "CapabilityBoundingSet=", "TasksMax=64",
                       "EnvironmentFile=-/etc/oracle-keepalive.conf",
                       "WantedBy=multi-user.target"):
            self.assertIn(needle, text)
        self.assertNotIn("CPUQuota", text)
        self.assertIn("ExecStart=/usr/bin/python3 "
                      "/usr/local/lib/oracle-keepalive/oracle-keepalive.py run", text)

    def test_config_example_documents_every_variable(self):
        text = (ROOT / "oracle-keepalive.conf.example").read_text(encoding="utf-8")
        for name in ("KEEPALIVE_MODE", "KEEPALIVE_CPU_DUTY", "KEEPALIVE_CPU_BURST_SECONDS",
                     "KEEPALIVE_CPU_WORKERS", "KEEPALIVE_MEM_TARGET", "KEEPALIVE_MEM_MAX_HOLD",
                     "KEEPALIVE_MEM_FLOOR", "KEEPALIVE_MEM_STEP_MB", "KEEPALIVE_TICK_SECONDS",
                     "KEEPALIVE_SAMPLE_SECONDS", "KEEPALIVE_STATE_DIR"):
            self.assertIn(name, text)
        # Every assignment must be commented out so the defaults apply.
        for line in text.splitlines():
            stripped = line.strip()
            if "=" in stripped and not stripped.startswith("#"):
                self.fail("uncommented assignment in example: %r" % stripped)

    def test_installer_supports_the_documented_flags(self):
        text = (ROOT / "install.sh").read_text(encoding="utf-8")
        for needle in ("--dry-run", "--uninstall", "--purge", "--replace-eat",
                       "set -euo pipefail", "systemctl daemon-reload",
                       "MemoryMax", "eat.service"):
            self.assertIn(needle, text)

    def test_program_has_no_network_or_subprocess_imports(self):
        src = PROGRAM.read_text(encoding="utf-8")
        for banned in ("import socket", "import subprocess", "import urllib",
                       "import http", "os.system", "import requests", "import ssl"):
            self.assertNotIn(banned, src)

    def test_systemd_analyze_verifies_the_unit_if_available(self):
        analyzer = shutil.which("systemd-analyze")
        if not analyzer:
            self.skipTest("systemd-analyze not available")
        tmp = tempfile.mkdtemp(prefix="ok-unit-")
        try:
            # systemd-analyze insists ExecStart exists; point it at the local copy.
            text = (ROOT / "oracle-keepalive.service").read_text(encoding="utf-8")
            text = "\n".join(
                "ExecStart=%s %s run" % (sys.executable, PROGRAM)
                if line.startswith("ExecStart=") else line
                for line in text.splitlines())
            unit = os.path.join(tmp, "oracle-keepalive.service")
            with open(unit, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
            proc = subprocess.run([analyzer, "verify", "--man=no", unit],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            err = (proc.stdout + proc.stderr).decode("utf-8", "replace")
            self.assertEqual(proc.returncode, 0, err)
            self.assertNotIn("Unknown key", err)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class CliTests(unittest.TestCase):
    def test_subcommands_exist(self):
        parser = ok.build_parser()
        for cmd in ("run", "status", "plan", "selftest"):
            ns = parser.parse_args([cmd])
            self.assertEqual(ns.command, cmd)

    def test_help_exits_zero(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as ctx:
            ok.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_program_runs_as_a_script(self):
        proc = subprocess.run([sys.executable, str(PROGRAM), "plan"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=dict(os.environ, KEEPALIVE_STATE_DIR="/nonexistent/ok",
                                       KEEPALIVE_MODE="cpu"))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn(b"plan", proc.stdout)


class StateDirGuardTests(unittest.TestCase):
    """A KEEPALIVE_STATE_DIR that is not absolute or holds '..' is refused."""

    MEMINFO = ok.parse_meminfo(MEMINFO_SAMPLE)

    def make(self, **env):
        return ok.load_config(env=env, machine="x86_64", ncpu=1, meminfo=self.MEMINFO)

    def test_problem_none_for_a_normal_path(self):
        self.assertIsNone(ok.state_dir_problem("/var/lib/oracle-keepalive"))
        self.assertIsNone(ok.state_dir_problem("/opt/x/state"))

    def test_problem_relative(self):
        self.assertIn("not an absolute", ok.state_dir_problem("state") or "")

    def test_problem_dotdot(self):
        self.assertIn("'..'", ok.state_dir_problem("/var/lib/../../etc") or "")

    def test_problem_root_and_empty(self):
        self.assertIsNotNone(ok.state_dir_problem("/"))
        self.assertIsNotNone(ok.state_dir_problem(""))

    def test_load_config_records_a_note_for_relative(self):
        cfg = self.make(KEEPALIVE_STATE_DIR="relative/state")
        self.assertFalse(cfg.state_dir_valid)
        self.assertEqual(cfg.state_dir, "relative/state")  # not silently rewritten
        self.assertTrue(any("KEEPALIVE_STATE_DIR" in n for n in cfg.notes), cfg.notes)

    def test_load_config_records_a_note_for_dotdot(self):
        cfg = self.make(KEEPALIVE_STATE_DIR="/var/lib/../etc")
        self.assertFalse(cfg.state_dir_valid)
        self.assertTrue(any("KEEPALIVE_STATE_DIR" in n for n in cfg.notes), cfg.notes)

    def test_load_config_accepts_a_normal_path(self):
        cfg = self.make(KEEPALIVE_STATE_DIR="/var/lib/ok-test")
        self.assertTrue(cfg.state_dir_valid)
        self.assertFalse(any("KEEPALIVE_STATE_DIR" in n for n in cfg.notes), cfg.notes)

    def _run(self, state_dir):
        cfg = self.make(KEEPALIVE_STATE_DIR=state_dir, KEEPALIVE_MODE="cpu")
        daemon = ok.Daemon(cfg)
        daemon.stop = True  # if the guard ever disappears, fail instead of hanging
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = daemon.run()
        return rc, buf.getvalue()

    def test_run_refuses_a_relative_state_dir(self):
        rel = "ok-relative-%s" % uuid.uuid4().hex[:8]
        self.addCleanup(shutil.rmtree, os.path.abspath(rel), True)
        rc, out = self._run(rel)
        self.assertEqual(rc, 1, out)
        self.assertIn("ERROR", out)
        self.assertIn("KEEPALIVE_STATE_DIR", out)
        self.assertFalse(os.path.exists(rel), "run() must not create a relative state dir")

    def test_run_refuses_a_dotdot_state_dir(self):
        rc, out = self._run("/var/lib/../etc/ok-test")
        self.assertEqual(rc, 1, out)
        self.assertIn("ERROR", out)


class StartupSelfCheckTests(unittest.TestCase):
    """A pidfile left behind by SIGKILL/OOM must not block the next start."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ok-proc-")
        self.proc_root = os.path.join(self.tmp, "proc")
        os.makedirs(self.proc_root)
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self._proc_root = ok.PROC_ROOT
        self.children = []

    def tearDown(self):
        ok.PROC_ROOT = self._proc_root
        for proc in self.children:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _child(self, script="import time; time.sleep(30)"):
        proc = subprocess.Popen([sys.executable, "-c", script])
        self.children.append(proc)
        return proc

    def _fake_cmdline(self, pid, argv):
        d = os.path.join(self.proc_root, str(pid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "cmdline"), "wb") as fh:
            fh.write(b"\0".join(a.encode() for a in argv) + b"\0")

    def _cfg(self):
        return ok.load_config(env={"KEEPALIVE_MODE": "cpu", "KEEPALIVE_CPU_WORKERS": "1",
                                   "KEEPALIVE_STATE_DIR": self.state},
                              machine="x86_64", ncpu=1,
                              meminfo=ok.parse_meminfo(MEMINFO_SAMPLE))

    def _daemon(self):
        daemon = ok.Daemon(self._cfg())
        daemon.stop = True  # never enter the burst loop
        return daemon

    # -- pid_is_ours ------------------------------------------------------
    def test_ours_when_cmdline_matches(self):
        pid = self._child().pid
        self._fake_cmdline(pid, ["/usr/bin/python3",
                                 "/usr/local/lib/oracle-keepalive/oracle-keepalive.py", "run"])
        self.assertTrue(ok.pid_is_ours(pid, self.proc_root))

    def test_not_ours_when_cmdline_differs(self):
        pid = self._child().pid
        self._fake_cmdline(pid, ["/usr/bin/python3", "-c", "import time; time.sleep(30)"])
        self.assertFalse(ok.pid_is_ours(pid, self.proc_root))

    def test_not_ours_when_cmdline_unreadable(self):
        pid = self._child().pid
        self.assertIsNone(ok.pid_cmdline(pid, self.proc_root))
        self.assertFalse(ok.pid_is_ours(pid, self.proc_root))

    def test_not_ours_for_self_or_parent(self):
        self._fake_cmdline(os.getpid(), ["oracle-keepalive.py", "run"])
        self._fake_cmdline(os.getppid(), ["oracle-keepalive.py", "run"])
        self.assertFalse(ok.pid_is_ours(os.getpid(), self.proc_root))
        self.assertFalse(ok.pid_is_ours(os.getppid(), self.proc_root))

    def test_not_ours_for_a_dead_pid(self):
        pid = self._child().pid
        self._fake_cmdline(pid, ["oracle-keepalive.py", "run"])
        self.children[0].kill()
        self.children[0].wait(timeout=5)
        self.assertFalse(ok.pid_is_ours(pid, self.proc_root))

    def test_default_proc_root_is_proc(self):
        self.assertFalse(ok.pid_is_ours(999999))

    # -- run() startup decision -------------------------------------------
    def test_run_refuses_when_the_pidfile_really_is_another_instance(self):
        pid = self._child().pid
        self._fake_cmdline(pid, ["/usr/bin/python3", "/usr/local/lib/oracle-keepalive/"
                                 "oracle-keepalive.py", "run"])
        ok.PROC_ROOT = self.proc_root
        ok.write_pidfile(self.state, pid)
        ok.touch_heartbeat(self.state)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self._daemon().run()
        out = buf.getvalue()
        self.assertEqual(rc, 1, out)
        self.assertIn("another instance is already running", out)
        self.assertEqual(ok.read_pidfile(self.state), pid, "must not touch a live pidfile")

    def test_run_continues_when_the_pid_was_recycled(self):
        pid = self._child().pid  # alive, but not our program
        ok.PROC_ROOT = self.proc_root
        ok.write_pidfile(self.state, pid)
        ok.touch_heartbeat(self.state)  # fresh heartbeat survives a SIGKILL
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self._daemon().run()
        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        self.assertIn("treating the pidfile as stale", out)
        self.assertIsNone(ok.read_pidfile(self.state), "the stale pidfile is cleaned up on exit")

    def test_run_continues_when_cmdline_is_unreadable(self):
        pid = self._child().pid  # no fake /proc entry at all
        ok.PROC_ROOT = self.proc_root
        ok.write_pidfile(self.state, pid)
        ok.touch_heartbeat(self.state)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self._daemon().run()
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertIn("treating the pidfile as stale", buf.getvalue())

    def test_run_continues_when_the_pidfile_holds_our_own_pid(self):
        ok.PROC_ROOT = self.proc_root
        ok.write_pidfile(self.state, os.getpid())
        ok.touch_heartbeat(self.state)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self._daemon().run()
        self.assertEqual(rc, 0, buf.getvalue())

    def test_status_still_reports_running_for_a_live_pidfile(self):
        """status keeps its old behaviour: no /proc cmdline check there."""
        pid = self._child().pid
        ok.write_pidfile(self.state, pid)
        ok.touch_heartbeat(self.state)
        state, got_pid, _age = ok.service_state(self.state, stale_after=300)
        self.assertTrue(state.startswith("running"), state)
        self.assertEqual(got_pid, pid)


class InstallerFunctionTests(unittest.TestCase):
    """The rm -rf guard functions, exercised straight from install.sh."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ok-funcs-")
        text = INSTALLER.read_text(encoding="utf-8")
        marker = 'if [ "${UNINSTALL}" -eq 1 ]; then'
        self.assertIn(marker, text)
        self.funcs = os.path.join(self.tmp, "functions.sh")
        with open(self.funcs, "w", encoding="utf-8") as fh:
            fh.write(text[:text.index(marker)])
        self.bash = shutil.which("bash")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bash(self, body, *args):
        if not self.bash:
            self.skipTest("bash not available")
        script = os.path.join(self.tmp, "run.sh")
        # install.sh parses "$@" and calls set -euo pipefail at the top, so the
        # positional parameters are parked while the function part is sourced.
        header = ('set -uo pipefail\n'
                  'inner=("$@")\nset --\n'
                  'source \"$FUNCS\"\n'
                  'set -- "${inner[@]}"\n')
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(header + body)
        env = dict(os.environ, FUNCS=self.funcs)
        return subprocess.run([self.bash, script] + list(args), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_safe_state_dir_accepts_only_plausible_paths(self):
        good = ["/var/lib/oracle-keepalive", "/var/cache/ok/state",
                "/opt/oracle-keepalive/state", "/var/lib/a/b/c"]
        bad = ["/", "", "/var", "/usr", "/var/lib", "/opt/x", "/etc/oracle-keepalive",
               "/var/lib/../../etc", "../../etc", "relative/state", "/home/hermes/state"]
        body = ('for p in "$@"; do\n'
                '  if safe_state_dir "$p" 2>/dev/null; then printf "OK %s\\n" "$p"; '
                'else printf "NO %s\\n" "$p"; fi\n'
                'done\n')
        proc = self._bash(body, *good, *bad)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        lines = proc.stdout.decode().splitlines()
        self.assertEqual(len(lines), len(good) + len(bad), lines)
        verdicts = {}
        for line in lines:
            verdict, _sep, path = line.partition(" ")
            verdicts[path] = verdict
        for path in good:
            self.assertEqual(verdicts[path], "OK", path)
        for path in bad:
            self.assertEqual(verdicts[path], "NO", path)

    def test_safe_state_dir_explains_itself(self):
        proc = self._bash("safe_state_dir / || true\n")
        self.assertIn(b"refusing to delete", proc.stderr)
        proc = self._bash("safe_state_dir /usr || true\n")
        self.assertIn(b"refusing to delete", proc.stderr)

    def test_remove_fixed_refuses_anything_but_the_constants(self):
        victim = os.path.join(self.tmp, "victim.txt")
        with open(victim, "w", encoding="utf-8") as fh:
            fh.write("keep me\n")
        proc = self._bash('if remove_fixed "$1"; then echo ACCEPTED; else echo REFUSED; fi\n',
                          victim)
        self.assertIn(b"REFUSED", proc.stdout)
        self.assertIn(b"not one of the fixed install paths", proc.stderr)
        self.assertTrue(os.path.exists(victim), "remove_fixed deleted a non-constant path")

    def test_is_fixed_target_knows_the_constants(self):
        body = ('for p in "$LIB_DIR" "$UNIT_FILE" "$CONF_FILE" "$DROPIN_DIR" "$DROPIN_FILE"; do\n'
                '  is_fixed_target "$p" || { echo "MISS $p"; exit 3; }\n'
                'done\necho ALL-FIXED\n')
        proc = self._bash(body)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(b"ALL-FIXED", proc.stdout)


class InstallerSandboxTests(unittest.TestCase):
    """Run install.sh for real: a plain temp copy, and one with sandboxed paths."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ok-install-")
        self.sandbox = os.path.join(self.tmp, "sb")
        os.makedirs(self.sandbox)
        self.bash = shutil.which("bash")
        self.python = os.path.abspath(sys.executable)
        for name in ("oracle-keepalive.py", "oracle-keepalive.service",
                     "oracle-keepalive.conf.example"):
            shutil.copy2(os.path.join(str(ROOT), name), os.path.join(self.tmp, name))
        # Unmodified copy: --dry-run must be a no-op even for the real paths.
        self.installer = os.path.join(self.tmp, "install.sh")
        shutil.copy2(str(INSTALLER), self.installer)
        # Copy with every absolute path rewritten under the sandbox, so the
        # uninstall/purge code can be exercised without root.
        self.sandboxed = os.path.join(self.tmp, "install-sandboxed.sh")
        self._write_sandboxed_installer()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_sandboxed_installer(self):
        text = INSTALLER.read_text(encoding="utf-8")
        for needle in ("/usr/local/lib/${PROG}", "/etc/systemd/system/",
                       "/etc/oracle-keepalive.conf", "/var/lib/oracle-keepalive",
                       "/run/shm/MemoryEat.bin"):
            self.assertIn(needle, text)
            text = text.replace(needle, self.sandbox + needle)
        with open(self.sandboxed, "w", encoding="utf-8") as fh:
            fh.write(text)

    def env(self):
        return dict(os.environ, PYTHON=self.python, TMPDIR=self.tmp)

    def test_dry_run_final_lines_are_well_formed_and_touch_nothing(self):
        if not self.bash:
            self.skipTest("bash not available")
        if not shutil.which("systemctl"):
            self.skipTest("install.sh needs systemctl")
        watched = ["/etc/oracle-keepalive.conf", "/usr/local/lib/oracle-keepalive",
                   "/var/lib/oracle-keepalive"]
        before = [os.path.exists(p) for p in watched]
        proc = subprocess.run([self.bash, self.installer, "--dry-run"], cwd=self.tmp,
                              env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out = proc.stdout.decode()
        self.assertEqual(proc.returncode, 0, out + proc.stderr.decode())
        lines = [ln for ln in out.splitlines() if ln.strip()]
        self.assertEqual(lines[-1], "dry run complete; nothing was changed")
        expected = (r"^\[dry-run\] \S+ /usr/local/lib/oracle-keepalive/oracle-keepalive\.py "
                    r"status  \(environment taken from /etc/oracle-keepalive\.conf\)$")
        self.assertRegex(lines[-2], expected)
        for line in lines:
            self.assertNotIn("%s", line, "unexpanded printf format: %r" % line)
            self.assertFalse(re.search(r"\(\s*$", line), "truncated line: %r" % line)
        self.assertNotRegex(out, r"status\s*\(.*status")
        self.assertEqual([os.path.exists(p) for p in watched], before)
        self.assertEqual(before, [False, False, False],
                         "this test assumes nothing is installed under /etc, /usr/local, /var/lib")

    def test_purge_skips_an_unsafe_state_dir_and_exits_non_zero(self):
        if not self.bash:
            self.skipTest("bash not available")
        conf = os.path.join(self.sandbox, "etc", "oracle-keepalive.conf")
        os.makedirs(os.path.dirname(conf))
        lib = os.path.join(self.sandbox, "usr", "local", "lib", "oracle-keepalive")
        os.makedirs(lib)
        with open(os.path.join(lib, "oracle-keepalive.py"), "w", encoding="utf-8") as fh:
            fh.write("# placeholder\n")
        with open(conf, "w", encoding="utf-8") as fh:
            fh.write("KEEPALIVE_STATE_DIR=/\n")
        proc = subprocess.run([self.bash, self.sandboxed, "--uninstall", "--purge", "--dry-run"],
                              cwd=self.tmp, env=self.env(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out = proc.stdout.decode()
        err = proc.stderr.decode()
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("refusing to delete", err)
        self.assertIn("state dir was left in place", err)
        self.assertIn("removed", out)                       # the rest still uninstalled
        self.assertIn(os.path.join(lib), out)
        self.assertIn("skipped the state dir", out)
        self.assertTrue(os.path.isdir(lib), "dry run must not delete anything")
        self.assertTrue(os.path.exists(conf))

    def test_purge_accepts_a_plausible_state_dir(self):
        if not self.bash:
            self.skipTest("bash not available")
        conf = os.path.join(self.sandbox, "etc", "oracle-keepalive.conf")
        os.makedirs(os.path.dirname(conf))
        with open(conf, "w", encoding="utf-8") as fh:
            fh.write("KEEPALIVE_STATE_DIR=/var/cache/oracle-keepalive-does-not-exist\n")
        proc = subprocess.run([self.bash, self.sandboxed, "--uninstall", "--purge", "--dry-run"],
                              cwd=self.tmp, env=self.env(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        err = proc.stderr.decode()
        self.assertEqual(proc.returncode, 0, proc.stdout.decode() + err)
        self.assertNotIn("refusing to delete", err)
        self.assertIn("was not present", proc.stdout.decode())


if __name__ == "__main__":
    unittest.main(verbosity=2)
