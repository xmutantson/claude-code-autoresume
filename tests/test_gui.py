#!/usr/bin/env python3
"""Tests for the status/countdown GUI layer.

DEFAULT run is HEADLESS -- it exercises the pure countdown/format + state
derivation logic (format_hms, derive_view) with NO display, so it is safe on a
CI / Task-Scheduler box and never pops a window:

    python tests/test_gui.py

The one brief REAL-WINDOW smoke test (opens the Tk status window, confirms the
live countdown label ticks with a synthetic PENDING state and that the Pause
button toggles the kill-switch stop-file, then AUTO-CLOSES the window) is opt-in:

    python tests/test_gui.py --smoke
"""

import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import autoresume as ar  # noqa: E402

_passed = 0
_failed = 0


def check(cond, msg):
    global _passed, _failed
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if cond:
        _passed += 1
    else:
        _failed += 1


# --------------------------------------------------------------------------- #
# Headless: format_hms                                                         #
# --------------------------------------------------------------------------- #

def test_format_hms():
    print("\n[1] format_hms -> HH:MM:SS (clamped, day-aware)")
    check(ar.format_hms(0) == "00:00:00", "0s -> 00:00:00")
    check(ar.format_hms(59) == "00:00:59", "59s -> 00:00:59")
    check(ar.format_hms(60) == "00:01:00", "60s -> 00:01:00")
    check(ar.format_hms(61) == "00:01:01", "61s -> 00:01:01")
    check(ar.format_hms(3599) == "00:59:59", "3599s -> 00:59:59")
    check(ar.format_hms(3661) == "01:01:01", "3661s -> 01:01:01")
    check(ar.format_hms(45.4) == "00:00:45", "45.4s rounds -> 00:00:45")
    check(ar.format_hms(45.6) == "00:00:46", "45.6s rounds -> 00:00:46")
    check(ar.format_hms(-10) == "00:00:00", "negative clamps -> 00:00:00")
    # weekly resets can be days out
    check(ar.format_hms(90061) == "1d 01:01:01", "90061s -> 1d 01:01:01")
    check(ar.format_hms(2 * 86400 + 5) == "2d 00:00:05", "2d+5s -> 2d 00:00:05")


# --------------------------------------------------------------------------- #
# Headless: derive_view (state derivation)                                     #
# --------------------------------------------------------------------------- #

def test_derive_view():
    now = 1_000_000.0

    print("\n[2] WATCHING (idle, no limit hit yet)")
    v = ar.derive_view({"state": "WATCHING"}, now)
    check(v["headline"] == "WATCHING", "headline WATCHING")
    check(v["countdown"] is None, "no countdown when idle")
    check(v["status_line"] == "Watching — no limit hit yet", "idle status text")

    print("\n[3] PENDING (counting down to reset+buffer)")
    v = ar.derive_view({
        "state": "PENDING", "stopped": False, "kind": "weekly",
        "reset_str": "Jul 3, 1am (America/Los_Angeles)",
        "reset_epoch": now + 60, "fire_at": now + 105,
    }, now)
    check(v["headline"] == "PENDING", "headline PENDING")
    check(v["countdown"] == "00:01:45", f"countdown = fire_at-now = 00:01:45 (got {v['countdown']})")
    check(abs(v["remaining"] - 105.0) < 1e-6, "remaining seconds exposed")
    check("weekly" in v["detail"], "detail names the KIND")
    check("Jul 3, 1am" in v["detail"], "detail shows the raw reset string")
    check("counting down" in v["status_line"].lower(), "status says counting down")

    print("\n[4] PENDING overdue -> countdown clamps to 00:00:00 (never negative)")
    v = ar.derive_view({"state": "PENDING", "fire_at": now - 30}, now)
    check(v["countdown"] == "00:00:00", f"overdue clamps (got {v['countdown']})")

    print("\n[5] HELD (kill-switch) overrides headline, keeps the countdown visible")
    v = ar.derive_view({
        "state": "PENDING", "stopped": True, "kind": "session",
        "reset_str": "12am (America/Los_Angeles)",
        "reset_epoch": now + 10, "fire_at": now + 55,
    }, now)
    check(v["headline"] == "HELD", "headline HELD when stopped")
    check(v["countdown"] == "00:00:55", "countdown still rendered while HELD")
    check("kill-switch" in v["status_line"].lower(), "status explains the hold")

    print("\n[6] HELD while idle (kill-switch, no pending)")
    v = ar.derive_view({"state": "WATCHING", "stopped": True}, now)
    check(v["headline"] == "HELD", "headline HELD")
    check(v["countdown"] is None, "no countdown (nothing pending)")

    print("\n[7] FIRING and DONE")
    v = ar.derive_view({"state": "FIRING"}, now)
    check(v["headline"] == "FIRING", "headline FIRING")
    check("inject" in v["status_line"].lower(), "status says injecting")
    v = ar.derive_view({"state": "DONE"}, now)
    check(v["headline"] == "DONE", "headline DONE")
    check("watching for the next" in v["status_line"].lower(),
          "DONE status: injected, watching for next")


# --------------------------------------------------------------------------- #
# Headless: WatchStatus request/publish plumbing                              #
# --------------------------------------------------------------------------- #

def test_watch_status():
    print("\n[8] WatchStatus thread-safe publish + request flags")
    s = ar.WatchStatus()
    snap = s.snapshot()
    check(snap["state"] == "WATCHING", "initial state WATCHING")
    s.update(state="PENDING", fire_at=123.0, kind="weekly")
    snap = s.snapshot()
    check(snap["state"] == "PENDING" and snap["fire_at"] == 123.0, "update() published")
    s.set_last_log("hello")
    check(s.snapshot()["last_log"] == "hello", "set_last_log published")
    # request flags are one-shot (take clears)
    check(s.take_cancel() is False, "cancel initially unset")
    s.request_cancel()
    check(s.take_cancel() is True, "cancel taken once True")
    check(s.take_cancel() is False, "cancel cleared after take")
    s.request_inject_now()
    check(s.take_inject_now() is True and s.take_inject_now() is False,
          "inject-now is one-shot")


# --------------------------------------------------------------------------- #
# Opt-in: real Tk window smoke test (auto-closes)                             #
# --------------------------------------------------------------------------- #

def smoke():
    print("\n[SMOKE] real Tk status window: live countdown + Pause toggles stop-file")
    if ar.tk is None:
        print("  SKIP  tkinter unavailable")
        return
    tmp = os.path.join(tempfile.gettempdir(), f"ar_smoke_stop_{os.getpid()}.stop")
    if os.path.exists(tmp):
        os.remove(tmp)

    shared = ar.WatchStatus()
    now = time.time()
    shared.update(state="PENDING", stopped=False, kind="weekly",
                  reset_str="Jul 3, 1am (America/Los_Angeles)",
                  reset_epoch=now + 60, fire_at=now + 8,
                  last_log="smoke: synthetic PENDING seeded")

    root = ar.tk.Tk()
    # tick_ms=0 -> we drive refresh() manually so the test is deterministic.
    win = ar.StatusWindow(root, shared, tmp, tick_ms=0)
    # hard backstop so the window can never linger on the desktop
    root.after(6000, root.destroy)

    win.refresh()
    root.update()
    first = win.var_countdown.get()
    print(f"  countdown t0: {first}")
    check(first.startswith("00:00:0"), f"initial countdown ~8s (got {first})")

    time.sleep(1.2)
    win.refresh()
    root.update()
    second = win.var_countdown.get()
    print(f"  countdown t1: {second}")
    check(second != first, f"countdown ticked ({first} -> {second})")
    check(second < first, "countdown decreased")

    # Pause button toggles the kill-switch stop-file.
    check(not os.path.exists(tmp), "stop-file absent before Pause")
    win.toggle_pause()
    root.update()
    check(os.path.exists(tmp), "Pause created the stop-file")
    win.refresh()
    root.update()
    check(win.var_state.get() == "HELD", "state shows HELD while paused")
    check(win.var_pause.get() == "Resume", "button now says Resume")

    win.toggle_pause()
    root.update()
    check(not os.path.exists(tmp), "Resume removed the stop-file")

    root.destroy()
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except OSError:
        pass
    print("  window closed")


def main():
    smoke_only = "--smoke" in sys.argv[1:]
    test_format_hms()
    test_derive_view()
    test_watch_status()
    if smoke_only:
        smoke()
    print(f"\n==== {_passed} passed, {_failed} failed ====")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
