#!/usr/bin/env python3
"""Tests for the usage-API detection path (primary source).

All usage responses are MOCKED -- no real network call, no token is read. These
tests reproduce today's false-arm (89%/38% -> arm NONE) as a PASS, prove a
100%/exhausted quota arms at resets_at+buffer, drive the blocked-then-reset and
still-blocked-at-fire-time flows through the real run_watch loop (dry-run + a
fake clock so no wall-clock waiting), confirm the no-credentials auto-fallback to
the transcript source, and check ISO8601 tz parsing.

Run:  python -m pytest tests/test_usage_api.py -q
"""

import os
import sys
from datetime import timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # import the package modules

import autoresume as ar          # noqa: E402
import usage_api                 # noqa: E402


# --------------------------------------------------------------------------- #
# Mocked usage responses (schema verified live on this machine)               #
# --------------------------------------------------------------------------- #

SESSION_RESET = "2026-07-02T07:10:00+00:00"
WEEKLY_RESET = "2026-07-03T08:00:00+00:00"


def _sample(session_pct, session_sev, weekly_pct=38, weekly_sev="normal"):
    return {
        "five_hour": {"utilization": float(session_pct), "resets_at": SESSION_RESET,
                      "limit_dollars": None, "severity": session_sev},
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


# The regression that reproduces today's false-arm: 89% session / 38% weekly.
SAMPLE_89_38 = _sample(89, "warning")
# The live shape we actually observed (91% is severity "critical" but < 100).
SAMPLE_91_CRITICAL = _sample(91, "critical")
# A blocked session (100% / exhausted).
SAMPLE_100 = _sample(100, "exhausted")
# After the session reset has been applied (utilization dropped).
SAMPLE_RESET = _sample(12, "normal")


# --------------------------------------------------------------------------- #
# Test doubles                                                                 #
# --------------------------------------------------------------------------- #

class FakeClient:
    """Scripted usage client: returns responses in order, sticky on the last.
    An entry that is an Exception instance is raised instead of returned."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def fetch(self):
        self.calls += 1
        r = self._responses[min(self.calls - 1, len(self._responses) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


class StopLoop(Exception):
    """Raised by the fake clock to break run_watch's infinite loop in tests."""


class FakeClock:
    """Controls autoresume's notion of time; advances on sleep, and aborts the
    loop after `max_sleeps` so a test never hangs."""

    def __init__(self, start, max_sleeps=200):
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


class FakeSource:
    """A minimal detection source: emits one blocked quota, then answers
    confirm_reset from a scripted True/False sequence."""

    def __init__(self, reset_epoch, confirm_seq):
        self.reset_epoch = reset_epoch
        self._confirm_seq = list(confirm_seq)
        self._polled = 0
        self.confirm_calls = 0

    def label(self):
        return "fake"

    def poll(self, now=None):
        self._polled += 1
        if self._polled == 1:
            return [{"kind": "session", "reset_str": "soon",
                     "reset_epoch": self.reset_epoch, "meta": {"label": "session"}}]
        return []

    def confirm_reset(self, pending, log):
        self.confirm_calls += 1
        v = self._confirm_seq.pop(0) if len(self._confirm_seq) > 1 else self._confirm_seq[0]
        return v, ("reset applied" if v else "still blocked")

    def loop_tick(self, pending, now, args):
        return 1


def _make_watch_args(tmp_path, **over):
    argv = [
        "watch", "--dry-run", "--source", "transcript",
        "--watch-dir", str(tmp_path / "watch"),
        "--state-file", str(tmp_path / "state.json"),
        "--log-file", str(tmp_path / "log.txt"),
        "--stop-file", str(tmp_path / "stop"),
        "--buffer", "5", "--poll", "1", "--min-interval", "0",
        "--confirm-interval", "1",
    ]
    os.makedirs(str(tmp_path / "watch"), exist_ok=True)
    args = ar.build_argparser().parse_args(argv)
    for k, v in over.items():
        setattr(args, k, v)
    return args


def _run_bounded(args, source, monkeypatch, start_epoch, max_sleeps=200):
    clock = FakeClock(start_epoch, max_sleeps=max_sleeps)
    monkeypatch.setattr(ar, "time", clock)
    try:
        ar.run_watch(args, source=source)
    except StopLoop:
        pass
    with open(args.log_file, encoding="utf-8") as fh:
        return fh.read(), clock


# --------------------------------------------------------------------------- #
# (a) 89%/38% -> arm NONE  (reproduces today's false-arm as a PASS)           #
# --------------------------------------------------------------------------- #

def test_89_38_does_not_arm():
    assert usage_api.find_blocked_quotas(SAMPLE_89_38) == []


def test_91_critical_does_not_arm():
    # 91% severity "critical" is the real live shape -- still below 100 -> no arm.
    assert usage_api.find_blocked_quotas(SAMPLE_91_CRITICAL) == []


def test_source_poll_below_threshold_returns_no_hits():
    src = ar.UsageApiSource(
        _NS(), log=lambda m: None,
        client=FakeClient([SAMPLE_89_38]), monitor_check=lambda p: False,
    )
    assert src.poll(now=1000.0) == []


# --------------------------------------------------------------------------- #
# (b) 100%/exhausted -> arms; fire_at == resets_at + buffer                    #
# --------------------------------------------------------------------------- #

def test_100_exhausted_arms_one_session_quota():
    blocked = usage_api.find_blocked_quotas(SAMPLE_100)
    assert len(blocked) == 1, blocked
    q = blocked[0]
    assert q["label"] == "session"
    # limits[] 'session' and top-level 'five_hour' collapse to ONE quota.
    assert q["resets_at"] == usage_api.parse_resets_at(SESSION_RESET)


def test_fire_at_is_resets_at_plus_buffer():
    q = usage_api.find_blocked_quotas(SAMPLE_100)[0]
    reset_epoch = q["resets_at"].timestamp()
    fire_at = reset_epoch + ar.BUFFER_SECONDS
    assert reset_epoch == usage_api.parse_resets_at(SESSION_RESET).timestamp()
    assert fire_at == reset_epoch + 45


def test_source_poll_at_threshold_emits_hit_with_reset_epoch():
    src = ar.UsageApiSource(
        _NS(), log=lambda m: None,
        client=FakeClient([SAMPLE_100]), monitor_check=lambda p: False,
    )
    hits = src.poll(now=1000.0)
    assert len(hits) == 1
    assert hits[0]["kind"] == "session"
    assert hits[0]["reset_epoch"] == usage_api.parse_resets_at(SESSION_RESET).timestamp()


# --------------------------------------------------------------------------- #
# (c) blocked-then-reset -> injects exactly once                              #
# --------------------------------------------------------------------------- #

def test_blocked_then_reset_injects_once(tmp_path, monkeypatch):
    start = 1_900_000_000.0
    src = FakeSource(reset_epoch=start + 2, confirm_seq=[False, True])
    args = _make_watch_args(tmp_path)
    log, clock = _run_bounded(args, src, monkeypatch, start, max_sleeps=100)
    assert log.count("DRY-RUN would inject") == 1, log
    assert "DONE injected+recorded" in log
    # confirm was polled at least twice: once (not reset) then once (reset).
    assert src.confirm_calls >= 2


def test_run_watch_resolves_source_when_none(tmp_path, monkeypatch):
    # main() calls run_watch(args) with source=None -> it must resolve the source
    # via select_source and drive IT (regression: an internal name-shadow once
    # made the loop call the None parameter).
    start = 1_900_000_000.0
    src = FakeSource(reset_epoch=start + 2, confirm_seq=[True])
    monkeypatch.setattr(ar, "select_source", lambda a, log, **k: src)
    args = _make_watch_args(tmp_path)
    clock = FakeClock(start, max_sleeps=100)
    monkeypatch.setattr(ar, "time", clock)
    try:
        ar.run_watch(args)          # NOTE: no source= -> exercises select_source
    except StopLoop:
        pass
    with open(args.log_file, encoding="utf-8") as fh:
        log = fh.read()
    assert log.count("DRY-RUN would inject") == 1, log


def test_source_confirm_reset_true_when_utilization_drops():
    src = ar.UsageApiSource(
        _NS(), log=lambda m: None,
        client=FakeClient([SAMPLE_RESET]), monitor_check=lambda p: False,
    )
    pending = {"kind": "session", "meta": {"label": "session"}}
    confirmed, detail = src.confirm_reset(pending, log=lambda m: None)
    assert confirmed is True, detail


# --------------------------------------------------------------------------- #
# (d) still-blocked at fire time -> waits/retries, never injects              #
# --------------------------------------------------------------------------- #

def test_still_blocked_never_injects(tmp_path, monkeypatch):
    start = 1_900_000_000.0
    src = FakeSource(reset_epoch=start + 2, confirm_seq=[False])  # never resets
    args = _make_watch_args(tmp_path)
    log, clock = _run_bounded(args, src, monkeypatch, start, max_sleeps=60)
    assert log.count("DRY-RUN would inject") == 0, log
    assert "WAIT" in log and "reset not applied yet" in log
    assert src.confirm_calls >= 2


def test_source_confirm_reset_false_when_still_blocked():
    src = ar.UsageApiSource(
        _NS(), log=lambda m: None,
        client=FakeClient([SAMPLE_100]), monitor_check=lambda p: False,
    )
    pending = {"kind": "session", "meta": {"label": "session"}}
    confirmed, detail = src.confirm_reset(pending, log=lambda m: None)
    assert confirmed is False, detail


def test_confirm_reset_false_on_rate_limit():
    src = ar.UsageApiSource(
        _NS(), log=lambda m: None,
        client=FakeClient([usage_api.RateLimited(30)]), monitor_check=lambda p: False,
    )
    confirmed, detail = src.confirm_reset({"meta": {"label": "session"}}, lambda m: None)
    assert confirmed is False
    assert "429" in detail or "rate" in detail.lower()


# --------------------------------------------------------------------------- #
# (e) no credentials -> auto falls back to transcript                         #
# --------------------------------------------------------------------------- #

def test_auto_falls_back_to_transcript_without_credentials(tmp_path):
    args = _make_watch_args(tmp_path, source="auto")
    src = ar.select_source(args, log=lambda m: None, creds_available=lambda: False)
    assert isinstance(src, ar.TranscriptSource)


def test_auto_uses_usage_api_with_credentials(tmp_path):
    args = _make_watch_args(tmp_path, source="auto")
    src = ar.select_source(args, log=lambda m: None, creds_available=lambda: True)
    assert isinstance(src, ar.UsageApiSource)


def test_explicit_usage_api_falls_back_without_credentials(tmp_path):
    args = _make_watch_args(tmp_path, source="usage-api")
    src = ar.select_source(args, log=lambda m: None, creds_available=lambda: False)
    assert isinstance(src, ar.TranscriptSource)


# --------------------------------------------------------------------------- #
# (f) parse resets_at timezone correctly                                      #
# --------------------------------------------------------------------------- #

def test_parse_resets_at_utc_offset():
    dt = usage_api.parse_resets_at("2026-07-02T07:10:00+00:00")
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 7, 2, 7, 10)


def test_parse_resets_at_microseconds():
    dt = usage_api.parse_resets_at("2026-07-02T07:09:59.117970+00:00")
    assert dt is not None and dt.microsecond == 117970


def test_parse_resets_at_z_suffix_equals_offset():
    a = usage_api.parse_resets_at("2026-07-02T07:10:00Z")
    b = usage_api.parse_resets_at("2026-07-02T07:10:00+00:00")
    assert a == b


def test_parse_resets_at_honors_non_utc_offset():
    # 00:10 at -07:00 is the same instant as 07:10 UTC.
    west = usage_api.parse_resets_at("2026-07-02T00:10:00-07:00")
    utc = usage_api.parse_resets_at("2026-07-02T07:10:00+00:00")
    assert west == utc
    assert west.timestamp() == utc.timestamp()


def test_parse_resets_at_none_and_garbage():
    assert usage_api.parse_resets_at(None) is None
    assert usage_api.parse_resets_at("") is None
    assert usage_api.parse_resets_at("not-a-date") is None


# --------------------------------------------------------------------------- #
# Fast direct polling: the Usage Monitor NO LONGER changes our cadence         #
# (v0.3.0 removed the 900s courtesy backoff -- it let a hit+reset slip a gap). #
# --------------------------------------------------------------------------- #

def test_monitor_present_does_not_slow_poll_cadence():
    # Monitor running MUST NOT back us off any more: we always poll at self.normal.
    src = ar.UsageApiSource(
        _NS(), log=lambda m: None,
        client=FakeClient([SAMPLE_89_38]), monitor_check=lambda p: True,
    )
    src.poll(now=1000.0)
    assert src._interval == float(src.normal)


def test_monitor_absent_uses_normal_cadence():
    src = ar.UsageApiSource(
        _NS(), log=lambda m: None,
        client=FakeClient([SAMPLE_89_38]), monitor_check=lambda p: False,
    )
    src.poll(now=1000.0)
    assert src._interval == float(src.normal)


def test_default_poll_cadence_is_fast():
    # The direct poll cadence default is fast (~30s), not the old ~165s.
    assert ar.USAGE_POLL_INTERVAL <= 60
    src = ar.UsageApiSource(_NS(), log=lambda m: None,
                            client=FakeClient([SAMPLE_89_38]))
    assert src.normal == float(ar.USAGE_POLL_INTERVAL) or src.normal == ar.USAGE_POLL_INTERVAL


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #

class _NS:
    """A tiny args stand-in with the attributes UsageApiSource reads (defaults)."""
    usage_poll = ar.USAGE_POLL_INTERVAL
    monitor_poll = ar.MONITOR_POLL_INTERVAL
    confirm_below = ar.CONFIRM_BELOW
    usage_monitor_proc = ar.USAGE_MONITOR_PROC
    on_auth_expired = "log"
    cred_path = None
