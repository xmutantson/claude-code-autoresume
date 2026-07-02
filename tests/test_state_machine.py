#!/usr/bin/env python3
"""Tests for the v0.3.0 ARM-ON-HIT / FIRE-ON-RESET state machine + manual mode.

All usage responses are MOCKED (a clock-aware fake client -- the window reads
>=100% before its reset instant and drops afterwards, exactly like the real
monotonic-until-reset quota); no real network, no token is read. A fake clock
drives run_watch's whole loop so nothing waits on the wall clock.

Covers the owner's four required cases:
  (a) hit -> reset  ARMS then FIRES exactly once.
  (b) benign reset  (window <100% throughout, NO prior observed hit) NEVER fires.
  (c) a GUI-set manual time FIRES at that time (via the shared manual file).
  (d) arm-state PERSISTS across a simulated watcher restart (a reset that landed
      while the watcher was down fires on the next poll).

Run:  python -m pytest tests/test_state_machine.py -q
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import autoresume as ar          # noqa: E402
import usage_api                 # noqa: E402

SESSION_RESET = "2026-07-02T07:10:00+00:00"
WEEKLY_RESET = "2026-07-09T07:10:00+00:00"
RESET_EPOCH = usage_api.parse_resets_at(SESSION_RESET).timestamp()


def _sample(session_pct, session_sev, weekly_pct=38, weekly_sev="normal"):
    return {
        "five_hour": {"utilization": float(session_pct), "resets_at": SESSION_RESET,
                      "severity": session_sev},
        "seven_day": {"utilization": float(weekly_pct), "resets_at": WEEKLY_RESET,
                      "severity": weekly_sev},
        "seven_day_opus": None, "seven_day_sonnet": None, "seven_day_cowork": None,
        "limits": [
            {"kind": "session", "group": "session", "percent": session_pct,
             "severity": session_sev, "resets_at": SESSION_RESET, "is_active": True},
            {"kind": "weekly_all", "group": "weekly", "percent": weekly_pct,
             "severity": weekly_sev, "resets_at": WEEKLY_RESET, "is_active": False},
        ],
        "extra_usage": {}, "spend": {}, "member_dashboard_available": False,
    }


class StopLoop(Exception):
    pass


class FakeClock:
    def __init__(self, start, max_sleeps=400):
        self.t = float(start)
        self.sleeps = 0
        self.max_sleeps = max_sleeps

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, float(s))
        self.sleeps += 1
        if self.sleeps > self.max_sleeps:
            raise StopLoop()


class ClockedUsageClient:
    """Time-aware fake usage client: the session window reads `blocked_pct`
    (>=100 by default) until `reset_at`, then `after_pct` (a real reset). This is
    faithful to a monotonic-until-reset quota. fetch() is called by BOTH the arm
    poll and the fire-on-clear confirm re-poll, and they see one consistent world."""

    def __init__(self, clock, reset_at, blocked_pct=100, after_pct=12):
        self.clock = clock
        self.reset_at = reset_at
        self.blocked_pct = blocked_pct
        self.after_pct = after_pct
        self.calls = 0

    def fetch(self):
        self.calls += 1
        if self.clock.time() >= self.reset_at:
            return _sample(self.after_pct, "normal")
        sev = "exhausted" if self.blocked_pct >= 100 else "warning"
        return _sample(self.blocked_pct, sev)


class SilentSource:
    """A source that never emits a hit (for manual-only tests). loop_tick is a
    small fixed tick so the fake clock advances one second at a time."""

    def label(self):
        return "silent"

    def poll(self, now=None):
        return []

    def confirm_reset(self, pending, log):
        return False, "silent"

    def loop_tick(self, pending, now, args):
        return 1


def _make_args(tmp_path, **over):
    argv = [
        "watch", "--dry-run", "--source", "transcript",
        "--watch-dir", str(tmp_path / "watch"),
        "--state-file", str(tmp_path / "state.json"),
        "--log-file", str(tmp_path / "log.txt"),
        "--stop-file", str(tmp_path / "stop"),
        "--manual-file", str(tmp_path / "manual.json"),
        "--buffer", "5", "--poll", "1", "--min-interval", "0",
        "--poll-interval", "2", "--confirm-interval", "2",
    ]
    os.makedirs(str(tmp_path / "watch"), exist_ok=True)
    args = ar.build_argparser().parse_args(argv)
    for k, v in over.items():
        setattr(args, k, v)
    return args


def _run_bounded(args, source, monkeypatch, start, max_sleeps=400):
    clock = FakeClock(start, max_sleeps=max_sleeps)
    monkeypatch.setattr(ar, "time", clock)
    try:
        ar.run_watch(args, source=source)
    except StopLoop:
        pass
    with open(args.log_file, encoding="utf-8") as fh:
        return fh.read(), clock


# --------------------------------------------------------------------------- #
# (a) hit -> reset ARMS then FIRES once                                        #
# --------------------------------------------------------------------------- #

def test_a_hit_then_reset_arms_then_fires_once(tmp_path, monkeypatch):
    start = RESET_EPOCH - 20            # 20s before the window resets
    clock = FakeClock(start, max_sleeps=400)
    monkeypatch.setattr(ar, "time", clock)
    client = ClockedUsageClient(clock, reset_at=RESET_EPOCH)
    args = _make_args(tmp_path)
    src = ar.UsageApiSource(args, log=lambda m: None, client=client)
    try:
        ar.run_watch(args, source=src)
    except StopLoop:
        pass
    with open(args.log_file, encoding="utf-8") as fh:
        log = fh.read()

    assert "ARM " in log, log
    assert "armed on observed hit, persisted" in log
    assert log.count("DRY-RUN would inject") == 1, log
    assert "FIRE inject" in log and "window cleared" in log
    assert "DONE injected+recorded" in log
    # arm cleared from the persisted state after firing.
    st = ar.load_state(args.state_file)
    assert st.get("arm") is None
    # fired exactly one session reset (dedup watermark holds one key).
    assert any(k.startswith("session|") for k in st.get("handled", []))


# --------------------------------------------------------------------------- #
# (b) benign reset with NO prior hit NEVER fires                              #
# --------------------------------------------------------------------------- #

def test_b_benign_reset_never_fires(tmp_path, monkeypatch):
    # Window sits at 89% (never >=100) then drops to 12% -- a reset with NO
    # observed hit. Must not arm, must not fire.
    start = RESET_EPOCH - 20
    clock = FakeClock(start, max_sleeps=120)
    monkeypatch.setattr(ar, "time", clock)
    client = ClockedUsageClient(clock, reset_at=RESET_EPOCH,
                                blocked_pct=89, after_pct=0)
    args = _make_args(tmp_path)
    src = ar.UsageApiSource(args, log=lambda m: None, client=client)
    try:
        ar.run_watch(args, source=src)
    except StopLoop:
        pass
    with open(args.log_file, encoding="utf-8") as fh:
        log = fh.read()

    assert "ARM " not in log, log
    assert "FIRE" not in log, log
    assert log.count("DRY-RUN would inject") == 0, log
    st = ar.load_state(args.state_file)
    assert st.get("arm") is None


# --------------------------------------------------------------------------- #
# (c) a GUI-set manual time FIRES at that time                                #
# --------------------------------------------------------------------------- #

def test_c_manual_time_fires_at_that_time(tmp_path, monkeypatch):
    start = 1_900_000_000.0
    manual_file = str(tmp_path / "manual.json")
    # This is exactly what the GUI "Set" button does under the hood.
    ar.write_manual_request(manual_file, resume_at=start + 5, manual_only=True)
    args = _make_args(tmp_path, manual_file=manual_file)
    log, clock = _run_bounded(args, SilentSource(), monkeypatch, start,
                              max_sleeps=60)
    assert "FIRE manual resume" in log, log
    assert log.count("DRY-RUN would inject") == 1, log
    assert "DONE injected+recorded manual|" in log
    # the manual request file is consumed after firing.
    assert ar.read_manual_request(manual_file) is None
    # fired at/after the scheduled time.
    assert clock.time() >= start + 5


def test_c_manual_does_not_fire_before_time(tmp_path, monkeypatch):
    start = 1_900_000_000.0
    manual_file = str(tmp_path / "manual.json")
    ar.write_manual_request(manual_file, resume_at=start + 1000, manual_only=True)
    args = _make_args(tmp_path, manual_file=manual_file)
    log, clock = _run_bounded(args, SilentSource(), monkeypatch, start,
                              max_sleeps=30)
    assert log.count("DRY-RUN would inject") == 0, log
    # still scheduled (never reached).
    assert ar.read_manual_request(manual_file) is not None


# --------------------------------------------------------------------------- #
# (d) arm-state PERSISTS across a simulated restart                           #
# --------------------------------------------------------------------------- #

def test_d_arm_persists_across_restart(tmp_path, monkeypatch):
    args = _make_args(tmp_path)

    # -- Phase 1: hit observed, ARM + persist, but reset never comes -> no fire.
    start1 = RESET_EPOCH - 20
    clock1 = FakeClock(start1, max_sleeps=8)         # stop well before any reset
    monkeypatch.setattr(ar, "time", clock1)
    # reset_at far in the future so the window stays blocked for the whole run.
    client1 = ClockedUsageClient(clock1, reset_at=RESET_EPOCH + 10 ** 6)
    src1 = ar.UsageApiSource(args, log=lambda m: None, client=client1)
    try:
        ar.run_watch(args, source=src1)
    except StopLoop:
        pass
    with open(args.log_file, encoding="utf-8") as fh:
        log1 = fh.read()
    assert "ARM " in log1, log1
    assert log1.count("DRY-RUN would inject") == 0, log1     # never fired
    persisted = ar.load_state(args.state_file).get("arm")
    assert isinstance(persisted, dict) and persisted.get("key"), persisted
    armed_key = persisted["key"]

    # -- Phase 2: RESTART. The reset landed while we were "down": the client now
    #    reports the window cleared. A fresh run_watch reloads the arm and fires.
    start2 = RESET_EPOCH + 5                          # now past the reset
    clock2 = FakeClock(start2, max_sleeps=60)
    monkeypatch.setattr(ar, "time", clock2)
    client2 = ClockedUsageClient(clock2, reset_at=RESET_EPOCH)   # already cleared
    src2 = ar.UsageApiSource(args, log=lambda m: None, client=client2)
    try:
        ar.run_watch(args, source=src2)
    except StopLoop:
        pass
    with open(args.log_file, encoding="utf-8") as fh:
        log2 = fh.read()
    assert "RESUME persisted arm" in log2, log2
    assert log2.count("DRY-RUN would inject") == 1, log2      # fired once on restart
    assert "DONE injected+recorded" in log2
    st = ar.load_state(args.state_file)
    assert st.get("arm") is None                              # disarmed
    assert armed_key in st.get("handled", [])                # once-per-reset


# --------------------------------------------------------------------------- #
# parse_resume_at: HH:MM | +Nm | ISO                                          #
# --------------------------------------------------------------------------- #

def test_parse_resume_at_relative_minutes():
    from datetime import datetime, timezone
    now = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc).astimezone()
    got = ar.parse_resume_at("+15m", now=now)
    assert abs(got - (now.timestamp() + 15 * 60)) < 1e-6
    assert abs(ar.parse_resume_at("+90", now=now) - (now.timestamp() + 90 * 60)) < 1e-6
    assert abs(ar.parse_resume_at("+2h", now=now) - (now.timestamp() + 7200)) < 1e-6
    assert abs(ar.parse_resume_at("+30s", now=now) - (now.timestamp() + 30)) < 1e-6


def test_parse_resume_at_hhmm_next_occurrence():
    from datetime import datetime
    now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    # 13:30 today is in the future -> today.
    later = ar.parse_resume_at("13:30", now=now)
    from datetime import datetime as _dt
    got = _dt.fromtimestamp(later)
    assert (got.hour, got.minute) == (13, 30)
    assert got.date() == now.date()
    # 11:00 already passed -> tomorrow.
    earlier = ar.parse_resume_at("11:00", now=now)
    got2 = _dt.fromtimestamp(earlier)
    assert (got2.hour, got2.minute) == (11, 0)
    assert got2.date() > now.date()


def test_parse_resume_at_iso_and_errors():
    import pytest
    got = ar.parse_resume_at("2026-07-02T15:10:00")
    from datetime import datetime as _dt
    assert _dt.fromtimestamp(got).strftime("%H:%M") == "15:10"
    for bad in ("", "not-a-time", "25:00", "12:99"):
        with pytest.raises(ValueError):
            ar.parse_resume_at(bad)


def test_manual_request_roundtrip(tmp_path):
    p = str(tmp_path / "m.json")
    assert ar.read_manual_request(p) is None
    ar.write_manual_request(p, 1_900_000_000.5, manual_only=True)
    got = ar.read_manual_request(p)
    assert got["resume_at"] == 1_900_000_000.5 and got["manual_only"] is True
    ar.clear_manual_request(p)
    assert ar.read_manual_request(p) is None
