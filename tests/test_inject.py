#!/usr/bin/env python3
"""End-to-end injection test into a throwaway window (deterministic).

Creates a real titled top-level window with a focused child classic EDIT control
(mirroring VS Code = a titled top-level window whose focused child is the
terminal), then drives the SAME guarded injection path used for the live
Claude Code window -- find_target_window -> activate -> foreground guard ->
type Unicode -> Enter -- and reads the EDIT's text back with WM_GETTEXT to prove
every character (and the submitting Enter) landed, in order.

Why a self-built window and not Notepad: the Win11 Notepad edit surface is a
DirectWrite RichEdit whose async composition garbles fast synthetic input and
whose "restore previous session" reopens stale buffers, so it is a
non-deterministic harness. A classic EDIT processes WM_CHAR synchronously and in
order -- a faithful, repeatable proxy. (Live injection into a real Notepad
window was also exercised during development and lands the full message + Enter;
that surface is simply not a stable automated gate.)

Run:  python tests/test_inject.py
"""

import ctypes
import os
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import autoresume as ar  # noqa: E402

user32 = ar.user32
kernel32 = ar.kernel32

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_VSCROLL = 0x00200000
ES_MULTILINE = 0x0004
ES_AUTOVSCROLL = 0x0040
PM_REMOVE = 0x0001
LRESULT = ctypes.c_ssize_t

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND), ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD), ("pt", POINT),
    ]


user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.RegisterClassW.argtypes = (ctypes.POINTER(WNDCLASS),)
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = (
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
)
user32.DestroyWindow.argtypes = (wintypes.HWND,)
user32.SetFocus.argtypes = (wintypes.HWND,)
user32.SetFocus.restype = wintypes.HWND
user32.PeekMessageW.argtypes = (ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT)
user32.TranslateMessage.argtypes = (ctypes.POINTER(MSG),)
user32.DispatchMessageW.argtypes = (ctypes.POINTER(MSG),)
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)


def _def_wndproc(hwnd, msg, wp, lp):
    return user32.DefWindowProcW(hwnd, msg, wp, lp)


_WNDPROC_KEEPALIVE = WNDPROC(_def_wndproc)  # must outlive the window


def pump(ms):
    end = time.time() + ms / 1000.0
    msg = MSG()
    while time.time() < end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.005)


def main():
    passed = failed = 0

    def check(cond, msg):
        nonlocal passed, failed
        print(("  PASS  " if cond else "  FAIL  ") + msg)
        if cond:
            passed += 1
        else:
            failed += 1

    hinst = kernel32.GetModuleHandleW(None)
    cls_name = f"ARInjectTestWin{os.getpid()}"
    wc = WNDCLASS()
    wc.lpfnWndProc = _WNDPROC_KEEPALIVE
    wc.hInstance = hinst
    wc.lpszClassName = cls_name
    wc.hbrBackground = wintypes.HBRUSH(6)  # COLOR_WINDOW+1
    if not user32.RegisterClassW(ctypes.byref(wc)):
        print(f"  FAIL  RegisterClassW failed err={ctypes.get_last_error()}")
        return 1

    title = f"ar-inject-test-{int(time.time())}"
    parent = user32.CreateWindowExW(
        0, cls_name, title, WS_OVERLAPPEDWINDOW | WS_VISIBLE,
        120, 120, 720, 360, None, None, hinst, None,
    )
    if not parent:
        print(f"  FAIL  create parent failed err={ctypes.get_last_error()}")
        return 1
    edit = user32.CreateWindowExW(
        0, "EDIT", "",
        WS_CHILD | WS_VISIBLE | WS_VSCROLL | ES_MULTILINE | ES_AUTOVSCROLL,
        0, 0, 700, 320, parent, None, hinst, None,
    )
    if not edit:
        print(f"  FAIL  create edit failed err={ctypes.get_last_error()}")
        user32.DestroyWindow(parent)
        return 1

    proc_img = ar._proc_image_basename(os.getpid())  # this process (python.exe)
    print(f"  window title={title!r} proc={proc_img} (parent + child EDIT)")

    try:
        user32.SetForegroundWindow(parent)
        user32.SetFocus(edit)
        pump(400)

        payload = ar.build_message("weekly", "Jul 3, 1am (America/Los_Angeles)")
        print(f"  payload ({len(payload)} chars): {payload}")

        # Drive the REAL guarded injection path against our own titled window.
        # focus_method="none": this hermetic harness focuses its own EDIT control
        # directly (SetFocus above); the Claude-extension focus gesture is out of
        # scope here and must not fire a global keybinding into the test window.
        ok, detail = ar.inject_message(
            payload,
            proc_name=proc_img,
            title_substr="ar-inject-test",
            prefer_substr=None,
            press_enter=True,
            focus_method="none",
            log=lambda m: print("    " + m),
        )
        check(ok, f"inject_message reported success (detail={detail})")

        # Re-focus the child edit (activate may have reset focus to the parent)
        # then dispatch the queued keystrokes.
        pump(1500)
        got = ar._window_title(edit)  # GetWindowText on an EDIT returns its text
        body = got.replace("\r", "").replace("\n", "")

        check(body == payload, "EDIT text equals the payload exactly (no dropped/garbled chars)")
        check(("\r\n" in got) or got.endswith("\n"),
              "Enter produced a newline in the buffer (submit keystroke landed)")
        if body != payload:
            print(f"  got : {got!r}")
    finally:
        user32.DestroyWindow(parent)
        pump(100)

    print(f"\n==== {passed} passed, {failed} failed ====")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
