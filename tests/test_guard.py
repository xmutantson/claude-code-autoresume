#!/usr/bin/env python3
"""Fail-safe tests: the correct-window guard must refuse to type unless the
FOREGROUND window is the intended target.

Covers:
  - no matching target window        -> inject aborts, nothing typed
  - foreground title mismatch        -> foreground_matches() == False
  - foreground process mismatch      -> foreground_matches() == False
  - foreground correct               -> foreground_matches() == True

Run:  python tests/test_guard.py
"""

import ctypes
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import autoresume as ar  # noqa: E402
import test_inject as ti  # reuse the parent+child window builder pieces  # noqa: E402


def main():
    passed = failed = 0

    def check(cond, msg):
        nonlocal passed, failed
        print(("  PASS  " if cond else "  FAIL  ") + msg)
        if cond:
            passed += 1
        else:
            failed += 1

    print("[1] No matching target window -> abort, type nothing")
    logs = []
    ok, detail = ar.inject_message(
        "THIS MUST NOT BE TYPED",
        proc_name="NoSuchProcess12345.exe",
        title_substr="no-such-window-title-zzz",
        prefer_substr=None,
        log=logs.append,
    )
    check(ok is False, "inject returned failure")
    check(detail == "no-target-window", f"detail == no-target-window (got {detail!r})")
    check(any("ABORT inject: no target window" in m for m in logs),
          "logged an ABORT with reason")

    # Build a real foreground window to probe the guard predicate directly.
    hinst = ti.kernel32.GetModuleHandleW(None)
    cls = f"ARGuardWin{os.getpid()}"
    wc = ti.WNDCLASS()
    wc.lpfnWndProc = ti._WNDPROC_KEEPALIVE
    wc.hInstance = hinst
    wc.lpszClassName = cls
    wc.hbrBackground = ctypes.wintypes.HBRUSH(6)
    ti.user32.RegisterClassW(ctypes.byref(wc))
    title = f"ar-guard-{int(time.time())}"
    win = ti.user32.CreateWindowExW(
        0, cls, title, ti.WS_OVERLAPPEDWINDOW | ti.WS_VISIBLE,
        140, 140, 500, 240, None, None, hinst, None,
    )
    proc_img = ar._proc_image_basename(os.getpid())
    try:
        ar.activate_window(win)
        ti.pump(500)
        got_fg = ar.user32.GetForegroundWindow() == win
        print(f"  brought our window to foreground: {got_fg}")

        print("\n[2] Foreground title mismatch -> guard False (would abort)")
        okg, fg_title = ar.foreground_matches(proc_name=proc_img,
                                              title_substr="THIS_TITLE_WONT_MATCH")
        check(okg is False, f"guard rejects wrong title (fg={fg_title!r})")

        print("\n[3] Foreground process mismatch -> guard False (would abort)")
        okg, fg_title = ar.foreground_matches(proc_name="SomeOtherProc999.exe",
                                              title_substr="ar-guard")
        check(okg is False, "guard rejects wrong process")

        print("\n[4] Foreground correct (right proc + title) -> guard True")
        if not got_fg:
            print("  SKIP  could not steal foreground in this environment "
                  "(OS foreground lock); guard-accept path exercised by test_inject.py")
        else:
            okg, fg_title = ar.foreground_matches(proc_name=proc_img,
                                                  title_substr="ar-guard")
            check(okg is True,
                  f"guard accepts the correct foreground window (fg={fg_title!r})")
    finally:
        ti.user32.DestroyWindow(win)
        ti.pump(100)

    print(f"\n==== {passed} passed, {failed} failed ====")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
