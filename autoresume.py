#!/usr/bin/env python3
"""
autoresume.py -- Claude Code auto-resume on usage-limit reset (Windows).

Watches the active Claude Code session transcript (JSONL) for a usage-limit-hit
entry, parses which KIND of limit was hit (session / weekly / monthly spend) and
the reset time (tz-aware), waits until the reset (+ a small buffer), then types a
plain-language "automated resume" message into the focused Claude Code chat
input in VS Code and presses Enter -- so the autonomous session picks
up where it left off without a human present.

Replaces a fixed-time blind-Enter script (the classic "send {Enter} at 1:15am"
AutoHotkey hack) with limit-aware detection, correct-window targeting,
send-once-per-reset dedup, a kill switch, and full logging.

Design: parse/schedule/dedup in Python (json + datetime + zoneinfo/tzdata);
injection via pure Win32 SendInput (no third-party deps). An AHK v2 fallback
injector (inject.ahk) is provided as an alternative.

No external Python packages are required beyond the standard library plus
`tzdata` (already present) for zoneinfo on Windows.
"""

from __future__ import annotations

import argparse
import ctypes
import glob
import json
import os
import re
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - py<3.9
    ZoneInfo = None  # type: ignore

# Tkinter is stdlib but imported guardedly so the module still imports (and the
# parse/inject/guard tests still run) on a Python without the tk bindings. The
# GUI mode degrades to headless watch if tk is unavailable.
try:  # pragma: no cover - trivial import guard
    import tkinter as tk
    from tkinter import font as tkfont
except Exception:  # pragma: no cover
    tk = None  # type: ignore
    tkfont = None  # type: ignore


# --------------------------------------------------------------------------- #
# Configuration defaults (override via CLI)                                    #
# --------------------------------------------------------------------------- #

def _default_watch_dir():
    """Auto-select the Claude Code project transcript directory to watch.

    Claude Code stores per-project transcripts under
    ``~/.claude/projects/<sanitized-cwd>/``. With no ``--watch-dir`` given, pick
    the project whose transcript changed most recently (i.e. the one you are
    actively working in). Falls back to the projects root if none are found.
    Override anytime with ``--watch-dir``.
    """
    projects = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    try:
        subdirs = [
            os.path.join(projects, d)
            for d in os.listdir(projects)
            if os.path.isdir(os.path.join(projects, d))
        ]
    except OSError:
        return projects

    def _latest_mtime(d):
        js = glob.glob(os.path.join(d, "*.jsonl"))
        return max((os.path.getmtime(f) for f in js), default=0.0)

    return max(subdirs, key=_latest_mtime) if subdirs else projects


DEFAULT_WATCH_DIR = _default_watch_dir()

# Foreground-window guard: only inject when the focused window is this process
# AND its title contains this substring. (VS Code = Code.exe, title "... Visual
# Studio Code".) PREFER_TITLE is a soft preference used when several match.
TARGET_PROC = "Code.exe"
TARGET_TITLE = "Visual Studio Code"
PREFER_TITLE = ""       # soft tie-breaker when several VS Code windows match:
                        # set to a substring of YOUR workspace title (e.g. your
                        # repo folder name) to disambiguate. Empty = no preference.

BUFFER_SECONDS = 45          # fire at reset + this (reset text is minute-granular,
                             # server clock can lag). Design brief: +30-60s.
POLL_INTERVAL = 5            # seconds between transcript polls (coarse, not tight)
MIN_INJECT_INTERVAL = 60     # global backstop: >=60s between any two injections
STALE_RESET_GRACE = 6 * 3600 # if a reset already passed by more than this, treat
                             # the entry as historical and do NOT inject.

_LOCALAPPDATA = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
STATE_DIR = os.path.join(_LOCALAPPDATA, "claude-autoresume")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LOG_FILE = os.path.join(STATE_DIR, "autoresume.log")
STOP_FILE = os.path.join(os.environ.get("TEMP", STATE_DIR), "autoresume.stop")

MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

# --------------------------------------------------------------------------- #
# Parsing: KIND + reset time from the limit-hit transcript line                #
# --------------------------------------------------------------------------- #

# The canonical assistant limit-hit line (byte-confirmed from real transcripts):
#   type=="assistant", isApiErrorMessage==true, error=="rate_limit",
#   message.content[0].text starts "You've hit your (session|weekly|monthly
#   spend) limit". Apostrophe is ASCII 0x27; separator is " · " (space +
#   U+00B7 middle dot + space). Keying on rate_limit/429 ALONE is wrong --
#   transient "Server is temporarily limiting requests"/"529 Overloaded" share
#   it; the text prefix disambiguates.
LIMIT_TEXT_RE = re.compile(r"^You've hit your (session|weekly|monthly spend) limit")
TZ_RE = re.compile(r"\(([^)]+)\)")
TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.IGNORECASE)
DATE_RE = re.compile(r"([A-Z][a-z]{2})\s+(\d{1,2})")


def extract_limit_text(obj: dict):
    """Return the limit-hit text string if `obj` is a genuine limit-hit
    assistant line, else None. Applies the full match predicate."""
    if not isinstance(obj, dict):
        return None
    if obj.get("type") != "assistant":
        return None
    if not obj.get("isApiErrorMessage"):
        return None
    if obj.get("error") != "rate_limit":
        return None
    try:
        text = obj["message"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(text, str):
        return None
    if not LIMIT_TEXT_RE.match(text.strip()):
        return None
    return text.strip()


def parse_limit_text(text: str):
    """Parse a limit-hit text into {kind, reset_str, text}.

    kind in {session, weekly, monthly spend}. reset_str is the exact substring
    after 'resets ' (e.g. '12am (America/Los_Angeles)' or
    'Jul 3, 1am (America/Los_Angeles)'), or None for monthly-spend (billing cap,
    no timed reset)."""
    m = LIMIT_TEXT_RE.match(text.strip())
    if not m:
        return None
    kind = m.group(1)
    reset_str = None
    # Split on the word 'resets ' so we are robust to the exact separator glyph.
    if "resets " in text:
        reset_str = text.split("resets ", 1)[1].strip()
    return {"kind": kind, "reset_str": reset_str, "text": text.strip()}


def _to_hour24(hour: int, ampm: str) -> int:
    ampm = ampm.lower()
    if ampm == "am":
        return 0 if hour == 12 else hour
    return 12 if hour == 12 else hour + 12


def resolve_reset_epoch(reset_str: str, now: datetime | None = None):
    """Resolve a reset string to (epoch_seconds, tz_aware_datetime).

    Handles:
      - session (time only): '12am (America/Los_Angeles)' -> next future
        occurrence of that wall-clock (today or tomorrow) in the stated tz.
      - weekly (date + time): 'Jul 3, 1am (America/Los_Angeles)' -> that date at
        that time; year inferred as the nearest future occurrence.
    The timezone in parens is the ACCOUNT's tz (may differ from the machine
    clock); the reset is resolved there and .timestamp() yields the correct
    machine-local epoch."""
    if reset_str is None:
        raise ValueError("no reset string (monthly-spend has no timed reset)")

    tzm = TZ_RE.search(reset_str)
    tz = None
    if tzm and ZoneInfo is not None:
        try:
            tz = ZoneInfo(tzm.group(1).strip())
        except Exception:
            tz = None
    if tz is None:
        tz = datetime.now().astimezone().tzinfo  # fall back to machine-local tz

    tm = TIME_RE.search(reset_str)
    if not tm:
        raise ValueError(f"no time in reset string: {reset_str!r}")
    hour = _to_hour24(int(tm.group(1)), tm.group(3))
    minute = int(tm.group(2)) if tm.group(2) else 0

    if now is None:
        now = datetime.now(timezone.utc)
    now_tz = now.astimezone(tz)

    dm = DATE_RE.search(reset_str)
    if dm and dm.group(1) in MONTHS:
        # Weekly: explicit month + day, infer year as nearest future occurrence.
        month = MONTHS[dm.group(1)]
        day = int(dm.group(2))
        cands = []
        for yr in (now_tz.year - 1, now_tz.year, now_tz.year + 1):
            try:
                cands.append(datetime(yr, month, day, hour, minute, tzinfo=tz))
            except ValueError:
                continue  # e.g. Feb 29 in a non-leap year
        grace = now_tz - timedelta(hours=12)
        future = [d for d in cands if d >= grace]
        dt = min(future) if future else min(
            cands, key=lambda d: abs((d - now_tz).total_seconds())
        )
    else:
        # Session: time only -> today, roll to tomorrow if already passed.
        dt = now_tz.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt <= now_tz:
            dt = dt + timedelta(days=1)

    return dt.timestamp(), dt


# --------------------------------------------------------------------------- #
# Win32 injection (pure ctypes, no third-party deps)                           #
# --------------------------------------------------------------------------- #

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = ctypes.wintypes.WPARAM

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_RETURN = 0x0D
VK_MENU = 0x12          # Alt
VK_CONTROL = 0x11       # Ctrl
VK_SHIFT = 0x10         # Shift
VK_P = 0x50             # 'P'  (Command Palette)
VK_K = 0x4B             # 'K'  (dedicated focus keybinding)
SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# --------------------------------------------------------------------------- #
# Claude Code extension: focus the chat input BEFORE typing                    #
#                                                                              #
# The owner runs the Claude Code VS Code EXTENSION (a chat panel), NOT the     #
# integrated terminal (claudeCode.useTerminal is unset -> default false).      #
# Foregrounding Code.exe does NOT put keyboard focus into the chat input, so   #
# the message must be typed AFTER explicitly focusing that input.              #
#                                                                              #
# Two selectable methods (FOCUS_METHOD):                                       #
#   "keybind" (A, default): SendInput a dedicated user keybinding bound to the #
#       extension command `claude-vscode.focus`. Add ONE entry to VS Code user #
#       keybindings.json (see tools/autoresume/README.md):                     #
#           { "key": "ctrl+alt+shift+k", "command": "claude-vscode.focus" }    #
#       Single keystroke, deterministic, no OS collision, and (with no         #
#       when-clause) it can never toggle to blur.                              #
#   "palette" (B, zero-config): SendInput Ctrl+Shift+P, type the command       #
#       title, Enter. Needs no keybindings.json edit; slower, title-dependent. #
#   "none": send no focus gesture (used by the hermetic injection self-test,   #
#       which focuses its own EDIT control directly).                          #
#                                                                              #
# Do NOT use raw Ctrl+Esc: it is the Windows Start-menu shell hotkey AND the   #
# extension's Ctrl+Esc binding is `editorTextFocus`-gated and TOGGLES to BLUR  #
# when the input already has focus -- unreliable. That is the owner-caught bug #
# this replaces.                                                               #
FOCUS_METHOD = "keybind"                       # "keybind" | "palette" | "none"
FOCUS_KEYBIND_MODS = (VK_CONTROL, VK_MENU, VK_SHIFT)   # Ctrl+Alt+Shift ...
FOCUS_KEYBIND_VK = VK_K                         # ... +K  -> ctrl+alt+shift+k
FOCUS_PALETTE_COMMAND = "Claude Code: Focus input"     # exact command title
FOCUS_SETTLE = 0.25            # settle (s) after the focus gesture before typing


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
user32.IsWindowVisible.argtypes = (wintypes.HWND,)
kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.QueryFullProcessImageNameW.argtypes = (
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
)
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
user32.BringWindowToTop.argtypes = (wintypes.HWND,)
user32.SystemParametersInfoW.argtypes = (
    wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT,
)
user32.SystemParametersInfoW.restype = wintypes.BOOL

SPI_GETFOREGROUNDLOCKTIMEOUT = 0x2000
SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
SPIF_SENDCHANGE = 0x0002

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _window_title(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _window_pid(hwnd) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _proc_image_basename(pid: int):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        size = wintypes.DWORD(512)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return None
    finally:
        kernel32.CloseHandle(h)


def find_target_window(proc_name=TARGET_PROC, title_substr=TARGET_TITLE,
                       prefer_substr=PREFER_TITLE):
    """Return (hwnd, title) of a visible window belonging to `proc_name` whose
    title contains `title_substr`. Windows containing `prefer_substr` win ties.
    Returns (None, None) if nothing matches."""
    matches = []

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_title(hwnd)
        if not title or title_substr.lower() not in title.lower():
            return True
        img = _proc_image_basename(_window_pid(hwnd))
        if img and img.lower() == proc_name.lower():
            matches.append((hwnd, title))
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    if not matches:
        return None, None
    if prefer_substr:
        for hwnd, title in matches:
            if prefer_substr.lower() in title.lower():
                return hwnd, title
    return matches[0]


def _kb(wVk, wScan, flags) -> INPUT:
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(wVk, wScan, flags, 0, 0)
    return inp


def _send(inputs) -> int:
    arr = (INPUT * len(inputs))(*inputs)
    return user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


TYPE_CHAR_DELAY = 0.020   # seconds between characters; a single large SendInput
                          # batch overruns ConPTY / the Win11 Notepad RichEdit
                          # (chars land as placeholders). Per-char pacing is the
                          # robust path and is imperceptible for a ~250-char line.


def type_unicode(text: str, char_delay: float = TYPE_CHAR_DELAY) -> int:
    """Type `text` as literal Unicode keystrokes into the focused window.

    Sends each character as its own down/up KEYEVENTF_UNICODE pair with a small
    inter-character delay so the receiving control (ConPTY terminal / RichEdit)
    reliably captures every character. Returns the number of INPUT events sent."""
    sent = 0
    for ch in text:
        code = ord(ch)
        sent += _send([
            _kb(0, code, KEYEVENTF_UNICODE),
            _kb(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        ])
        if char_delay:
            time.sleep(char_delay)
    return sent


def press_vk(vk: int) -> int:
    return _send([_kb(vk, 0, 0), _kb(vk, 0, KEYEVENTF_KEYUP)])


def key_combo(mod_vk: int, vk: int) -> int:
    """Press mod+key (e.g. Ctrl+A). Used by the injection self-test."""
    return _send([
        _kb(mod_vk, 0, 0),
        _kb(vk, 0, 0),
        _kb(vk, 0, KEYEVENTF_KEYUP),
        _kb(mod_vk, 0, KEYEVENTF_KEYUP),
    ])


def key_combo_multi(mod_vks, vk: int) -> int:
    """Press a chord: hold every modifier VK in `mod_vks` (in order), tap `vk`,
    then release the key and the modifiers in reverse. Used to invoke a VS Code
    keybinding such as Ctrl+Alt+Shift+K (focus) or Ctrl+Shift+P (palette)."""
    seq = [_kb(m, 0, 0) for m in mod_vks]
    seq.append(_kb(vk, 0, 0))
    seq.append(_kb(vk, 0, KEYEVENTF_KEYUP))
    seq.extend(_kb(m, 0, KEYEVENTF_KEYUP) for m in reversed(tuple(mod_vks)))
    return _send(seq)


def focus_claude_input(method=None, settle=None, log=None) -> bool:
    """Move keyboard focus INTO the Claude Code extension chat input.

    The autoresume target is the extension's chat PANEL, not the integrated
    terminal, so foregrounding Code.exe does not focus the input box. This sends
    the configured focus gesture so the subsequently-typed message lands in the
    chat input. Call it AFTER the foreground guard passes and BEFORE typing.

    method: "keybind" (send the dedicated Ctrl+Alt+Shift+K bound to
    `claude-vscode.focus`), "palette" (Command Palette -> the focus command
    title -> Enter), or "none" (no-op). Defaults to FOCUS_METHOD.

    Returns True (for logging); focus success can't be observed from userland,
    so this never blocks injection -- the correct-window guard already ran."""
    method = FOCUS_METHOD if method is None else method
    st = FOCUS_SETTLE if settle is None else settle

    def _log(m):
        if log:
            log(m)

    if method == "none":
        return True
    if method == "palette":
        # Ctrl+Shift+P opens the Command Palette (no OS collision; the palette
        # seizes keyboard focus), then type the command title and Enter.
        key_combo_multi((VK_CONTROL, VK_SHIFT), VK_P)
        time.sleep(st)
        type_unicode(FOCUS_PALETTE_COMMAND)
        time.sleep(st)
        press_vk(VK_RETURN)
        time.sleep(st)
        _log(f"FOCUS via Command Palette: {FOCUS_PALETTE_COMMAND!r}")
        return True
    # default: dedicated keybinding -> claude-vscode.focus
    key_combo_multi(FOCUS_KEYBIND_MODS, FOCUS_KEYBIND_VK)
    time.sleep(st)
    _log("FOCUS via keybind (ctrl+alt+shift+k -> claude-vscode.focus)")
    return True


def foreground_matches(proc_name=TARGET_PROC, title_substr=TARGET_TITLE):
    """Return (ok, fg_title) where ok is True iff the CURRENT foreground window
    belongs to proc_name and its title contains title_substr."""
    fg = user32.GetForegroundWindow()
    if not fg:
        return False, ""
    title = _window_title(fg)
    if title_substr.lower() not in title.lower():
        return False, title
    img = _proc_image_basename(_window_pid(fg))
    if not img or img.lower() != proc_name.lower():
        return False, title
    return True, title


def activate_window(hwnd) -> None:
    """Best-effort bring `hwnd` to the foreground, defeating the Windows
    foreground lock as far as userland allows (AttachThreadInput to the current
    foreground thread + temporarily zeroing SPI_SETFOREGROUNDLOCKTIMEOUT + an
    ALT tap so our process counts as having sent the last input). If the OS
    still refuses, the caller's foreground guard aborts rather than mistyping."""
    # ALT tap: makes our process eligible to set the foreground window.
    _send([_kb(VK_MENU, 0, 0), _kb(VK_MENU, 0, KEYEVENTF_KEYUP)])

    fg = user32.GetForegroundWindow()
    cur_tid = kernel32.GetCurrentThreadId()
    fg_tid = wintypes.DWORD()
    if fg:
        user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_tid))

    orig = wintypes.DWORD()
    user32.SystemParametersInfoW(SPI_GETFOREGROUNDLOCKTIMEOUT, 0,
                                 ctypes.byref(orig), 0)
    user32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0,
                                 ctypes.c_void_p(0), SPIF_SENDCHANGE)

    attached = False
    if fg_tid.value and fg_tid.value != cur_tid:
        attached = bool(user32.AttachThreadInput(cur_tid, fg_tid.value, True))

    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)

    if attached:
        user32.AttachThreadInput(cur_tid, fg_tid.value, False)

    # restore the original foreground-lock timeout
    user32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0,
                                 ctypes.c_void_p(orig.value), SPIF_SENDCHANGE)


def inject_message(message: str, proc_name=TARGET_PROC, title_substr=TARGET_TITLE,
                   prefer_substr=PREFER_TITLE, press_enter=True, settle=0.35,
                   focus_method=None, log=None):
    """Fail-safe injection: find the target window, activate it, VERIFY it is the
    foreground window (guard), FOCUS the Claude Code chat input, then type
    `message` and press Enter.

    focus_method selects how the chat input is focused (see focus_claude_input);
    None uses the module default FOCUS_METHOD.

    Returns (ok, detail). Never types into a non-target foreground window."""
    def _log(msg):
        if log:
            log(msg)

    hwnd, title = find_target_window(proc_name, title_substr, prefer_substr)
    if not hwnd:
        _log(f"ABORT inject: no target window (proc={proc_name} "
             f"title~{title_substr!r})")
        return False, "no-target-window"

    # Correct-window guard, with one activation retry.
    ok, fg_title = foreground_matches(proc_name, title_substr)
    if not ok:
        activate_window(hwnd)
        time.sleep(settle)
        ok, fg_title = foreground_matches(proc_name, title_substr)
    if not ok:
        _log(f"ABORT inject: foreground guard failed (fg={fg_title!r}); "
             f"refusing to type into a non-target window")
        return False, f"foreground-guard-failed:{fg_title!r}"

    time.sleep(settle)
    # Focus the Claude Code extension chat input BEFORE typing. The target is the
    # chat PANEL, not the integrated terminal -- foregrounding the window does not
    # focus the input, so without this the message would be typed into whatever
    # element last held focus (editor / webview). See focus_claude_input.
    focus_claude_input(method=focus_method, log=log)
    n = type_unicode(message)
    if press_enter:
        time.sleep(0.05)
        press_vk(VK_RETURN)
    _log(f"INJECTED into {fg_title!r}: typed {n} events, "
         f"enter={'yes' if press_enter else 'no'}")
    return True, fg_title


def inject_via_ahk(message: str, ahk_exe: str, ahk_script: str, log=None):
    """Fallback injector: shell out to inject.ahk (AutoHotkey v2)."""
    import subprocess
    if not (os.path.isfile(ahk_exe) and os.path.isfile(ahk_script)):
        if log:
            log(f"ABORT inject(ahk): missing exe/script ({ahk_exe} / {ahk_script})")
        return False, "ahk-missing"
    rc = subprocess.call([ahk_exe, ahk_script, message])
    if log:
        log(f"inject(ahk) rc={rc}")
    return rc == 0, f"ahk-rc-{rc}"


# --------------------------------------------------------------------------- #
# State, logging, kill switch                                                  #
# --------------------------------------------------------------------------- #


def _ensure_state_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def log_event(message: str, log_file=LOG_FILE, echo=True):
    _ensure_state_dir()
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    with open(log_file, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    if echo:
        # Under pythonw.exe (the windowless GUI launch) there is no console and
        # sys.stdout is None -> print() would raise. The file log is the source of
        # truth; console echo is best-effort.
        try:
            print(line, flush=True)
        except Exception:
            pass


def load_state(state_file=STATE_FILE) -> dict:
    try:
        with open(state_file, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"handled": []}


def save_state(state: dict, state_file=STATE_FILE):
    _ensure_state_dir()
    tmp = state_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, state_file)


def handled_key(kind: str, reset_epoch: float) -> str:
    # Round to the minute so retry-storm duplicates collapse to one key.
    return f"{kind}|{int(round(reset_epoch / 60.0)) * 60}"


def is_handled(state: dict, key: str) -> bool:
    return key in state.get("handled", [])


def mark_handled(state: dict, key: str):
    handled = state.setdefault("handled", [])
    if key not in handled:
        handled.append(key)
        # keep the list bounded
        if len(handled) > 200:
            del handled[:-200]


def kill_switch_active() -> bool:
    return os.path.exists(STOP_FILE)


# --------------------------------------------------------------------------- #
# Message                                                                      #
# --------------------------------------------------------------------------- #

RESUME_TEMPLATE = (
    "[AUTOMATED RESUME] A {kind} usage limit was hit and has now reset "
    "(reset reported: {reset_str}). This is an automated message; the user is "
    "away and will NOT see or respond to your output. Continue the autonomous "
    "work from where you left off: consult _research/AUTONOMOUS_WORKLOG.md and "
    "the current todo list, and keep going. Do not wait for user input."
)


def build_message(kind: str, reset_str: str) -> str:
    # kind reaching here is only session|weekly (monthly-spend never injects).
    return RESUME_TEMPLATE.format(kind=kind, reset_str=reset_str)


# --------------------------------------------------------------------------- #
# Transcript tailing                                                           #
# --------------------------------------------------------------------------- #


def newest_transcript(watch_dir: str):
    files = glob.glob(os.path.join(watch_dir, "*.jsonl"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


class Tailer:
    """Tail the newest *.jsonl in watch_dir, switching to a newer session file
    when one appears. Yields parsed limit-hit dicts for newly appended lines."""

    def __init__(self, watch_dir: str, from_start=False, log=log_event):
        self.watch_dir = watch_dir
        self.log = log
        self.path = newest_transcript(watch_dir)
        self.fh = None
        self._buf = ""
        if self.path:
            self.fh = open(self.path, "r", encoding="utf-8", errors="replace")
            if not from_start:
                self.fh.seek(0, os.SEEK_END)
            self.log(f"WATCH {self.path} (from_start={from_start})")

    def _maybe_switch(self):
        newest = newest_transcript(self.watch_dir)
        if newest and newest != self.path:
            self.log(f"SWITCH transcript -> {newest}")
            if self.fh:
                self.fh.close()
            self.path = newest
            self.fh = open(self.path, "r", encoding="utf-8", errors="replace")
            self._buf = ""
            # A freshly-created session file: read it from the beginning.

    def poll(self):
        """Return a list of parsed limit-hit dicts for newly completed lines."""
        results = []
        self._maybe_switch()
        if not self.fh:
            self.path = newest_transcript(self.watch_dir)
            if self.path:
                self.fh = open(self.path, "r", encoding="utf-8", errors="replace")
                self.log(f"WATCH {self.path} (from_start=True)")
            else:
                return results
        chunk = self.fh.read()
        if not chunk:
            return results
        self._buf += chunk
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if not line or '"isApiErrorMessage"' not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = extract_limit_text(obj)
            if not text:
                continue
            parsed = parse_limit_text(text)
            if parsed:
                parsed["timestamp"] = obj.get("timestamp")
                results.append(parsed)
        return results


# --------------------------------------------------------------------------- #
# Main watch loop                                                              #
# --------------------------------------------------------------------------- #


def run_watch(args, shared=None):
    """Watch the transcript and auto-resume on reset.

    `shared` is an optional WatchStatus the GUI reads: when provided, the loop
    PUBLISHES its state transitions to it (WATCHING/PENDING/FIRING/DONE + the
    fire_at the GUI counts down to) and honours GUI requests (cancel / inject-now)
    without forking any detection/parse/inject logic. When `shared` is None the
    behaviour is identical to before -- the headless path used by Task Scheduler
    and the tests is unchanged."""
    def publish(**kw):
        if shared is not None:
            shared.update(**kw)

    def log(m):
        log_event(m, args.log_file)
        if shared is not None:
            shared.set_last_log(m)

    log(f"START autoresume watch dir={args.watch_dir} buffer={args.buffer}s "
        f"injector={args.injector} dry_run={args.dry_run}")
    stop_path = args.stop_file
    if stop_path and os.path.exists(stop_path):
        log(f"NOTE kill switch present at {stop_path}; injection disabled until removed")
    publish(state="WATCHING", stopped=bool(stop_path and os.path.exists(stop_path)))

    state = load_state(args.state_file)
    tailer = Tailer(args.watch_dir, from_start=args.from_start, log=log)
    last_inject_time = 0.0

    # Pending injection: dict(kind, reset_str, reset_epoch, key, fire_at)
    pending = None
    kill_logged = False   # log kill-switch HOLD only on state change (no spam)

    def do_inject(kind, reset_str):
        nonlocal last_inject_time
        message = build_message(kind, reset_str)
        if args.dry_run:
            log(f"DRY-RUN would inject ({kind}): {message}")
            return True
        if args.injector == "ahk":
            ok, detail = inject_via_ahk(message, args.ahk_exe, args.ahk_script, log)
        else:
            ok, detail = inject_message(
                message, args.target_proc, args.target_title,
                args.prefer_title, focus_method=args.focus_method, log=log)
        last_inject_time = time.time()
        return ok

    while True:
        # Kill switch gates INJECTION only -- detection/logging keep running so
        # a hit that arrives while stopped is still recorded, and its pending
        # injection is HELD (not dropped) until the stop-file is removed.
        stopped = bool(args.stop_file and os.path.exists(args.stop_file))
        publish(stopped=stopped)

        # GUI request: cancel the current pending reset (mark it handled so it
        # never fires and drop it). No-op when headless / nothing pending.
        if shared is not None and shared.take_cancel():
            if pending is not None:
                log(f"CANCEL pending {pending['key']} by GUI request")
                mark_handled(state, pending["key"])
                save_state(state, args.state_file)
                pending = None
                publish(state="WATCHING", fire_at=None, kind=None, reset_str=None,
                        reset_epoch=None, last_action="cancelled pending reset")

        for hit in tailer.poll():
            kind = hit["kind"]
            if kind == "monthly spend":
                # No timed reset -> cannot auto-resume. Log once per day (the
                # retry storm repeats it) via the watermark, then skip.
                mkey = "monthly spend|" + datetime.now().strftime("%Y-%m-%d")
                if not is_handled(state, mkey):
                    log(f"DETECT monthly-spend limit (billing cap) -- CANNOT "
                        f"auto-resume, skipping. raw={hit['text']!r}")
                    mark_handled(state, mkey)
                    save_state(state, args.state_file)
                continue
            if not hit["reset_str"]:
                log(f"DETECT {kind} limit but no reset string; skipping. "
                    f"raw={hit['text']!r}")
                continue
            try:
                reset_epoch, reset_dt = resolve_reset_epoch(hit["reset_str"])
            except Exception as e:  # noqa: BLE001
                log(f"WARN could not resolve reset {hit['reset_str']!r}: {e}")
                continue
            key = handled_key(kind, reset_epoch)
            local_str = datetime.fromtimestamp(reset_epoch).isoformat(timespec="seconds")
            log(f"DETECT {kind} limit; reset_reported={hit['reset_str']!r} "
                f"resolved_local={local_str} key={key}")
            if is_handled(state, key):
                log(f"SKIP already handled {key} (retry-storm dedup)")
                continue
            now = time.time()
            if reset_epoch < now - args.stale_grace:
                log(f"SKIP stale reset {key} (already passed > "
                    f"{args.stale_grace}s ago; historical entry)")
                mark_handled(state, key)
                save_state(state, args.state_file)
                continue
            fire_at = reset_epoch + args.buffer
            # Prefer the latest (largest fire_at) if multiple pend.
            if pending is None or fire_at > pending["fire_at"]:
                pending = {
                    "kind": kind, "reset_str": hit["reset_str"],
                    "reset_epoch": reset_epoch, "key": key, "fire_at": fire_at,
                }
                log(f"PENDING inject {key} at {datetime.fromtimestamp(fire_at).isoformat(timespec='seconds')} "
                    f"(reset+{args.buffer}s)")
                publish(state="PENDING", kind=kind, reset_str=hit["reset_str"],
                        reset_epoch=reset_epoch, fire_at=fire_at)

        # GUI request: fire the current pending injection immediately (bypass the
        # remaining countdown). Still routed through the SAME guarded do_inject
        # below -- never a second unguarded path. Ignored while kill-switched.
        force_now = bool(shared is not None and shared.take_inject_now())

        # Fire pending injection when due.
        if pending is not None:
            now = time.time()
            if force_now or now >= pending["fire_at"]:
                if stopped:
                    # Kill switch present: HOLD the injection until it is lifted.
                    if not kill_logged:
                        log(f"HOLD injection {pending['key']}: kill switch present "
                            f"at {args.stop_file}; will inject once removed")
                        kill_logged = True
                    time.sleep(args.poll)
                    continue
                kill_logged = False
                if not force_now and now - last_inject_time < args.min_interval:
                    # backstop; wait out the remaining interval
                    time.sleep(min(args.poll, args.min_interval - (now - last_inject_time)))
                    continue
                key = pending["key"]
                if is_handled(state, key):
                    log(f"SKIP {key} became handled before fire")
                    pending = None
                    publish(state="DONE", fire_at=None)
                    continue
                log(f"FIRE inject {key} (kind={pending['kind']})"
                    + (" [inject-now]" if force_now else ""))
                publish(state="FIRING")
                ok = do_inject(pending["kind"], pending["reset_str"])
                if ok:
                    mark_handled(state, key)
                    save_state(state, args.state_file)
                    log(f"DONE injected+recorded {key}")
                    publish(state="DONE", fire_at=None,
                            last_action=f"injected {pending['kind']} ({key})")
                else:
                    log(f"RETRY-LATER inject failed for {key}; will retry next poll")
                    publish(state="PENDING", last_action=f"inject failed for {key}; retrying")
                    # leave pending so we retry; but avoid tight loop
                    time.sleep(args.poll)
                    continue
                pending = None

        time.sleep(args.poll)


# --------------------------------------------------------------------------- #
# GUI: status / countdown window (Tkinter, stdlib only)                        #
#                                                                              #
# The GUI is an ADDITIVE VIEW over the existing watch loop. The watch loop runs #
# in a background thread and PUBLISHES its transitions into a thread-safe        #
# WatchStatus; the Tk main thread reads snapshots and renders. Detection/parse/  #
# inject logic is NOT forked -- the countdown target is the SAME reset+buffer    #
# fire_at the loop already computes.                                            #
# --------------------------------------------------------------------------- #


class WatchStatus:
    """Thread-safe snapshot of the watch loop's state for the GUI.

    The watch loop is the sole WRITER of the status fields (via update()/
    set_last_log()); the GUI is the sole READER (via snapshot()). The GUI is the
    writer of the two request flags (cancel / inject-now); the watch loop reads +
    clears them via take_cancel()/take_inject_now(). All access is under one lock.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._d = {
            "state": "WATCHING",   # WATCHING / PENDING / FIRING / DONE
            "stopped": False,      # kill switch (stop-file) present
            "kind": None,          # session / weekly
            "reset_str": None,     # raw reset text, e.g. 'Jul 3, 1am (America/...)'
            "reset_epoch": None,   # resolved local reset instant (epoch seconds)
            "fire_at": None,       # reset_epoch + buffer -> the countdown TARGET
            "last_log": "",        # last log line the loop emitted
            "last_action": "",     # last consequential action
        }
        self._cancel = False
        self._inject_now = False

    def update(self, **kw):
        with self._lock:
            self._d.update(kw)

    def set_last_log(self, msg):
        with self._lock:
            self._d["last_log"] = msg

    def snapshot(self):
        with self._lock:
            return dict(self._d)

    # GUI -> loop requests -------------------------------------------------- #
    def request_cancel(self):
        with self._lock:
            self._cancel = True

    def request_inject_now(self):
        with self._lock:
            self._inject_now = True

    def take_cancel(self) -> bool:
        with self._lock:
            v, self._cancel = self._cancel, False
            return v

    def take_inject_now(self) -> bool:
        with self._lock:
            v, self._inject_now = self._inject_now, False
            return v


def format_hms(seconds) -> str:
    """Format a seconds count as HH:MM:SS (adds a 'Nd ' day prefix past 24h).

    Negative inputs clamp to zero -- an overdue countdown reads 00:00:00, never a
    negative time. Pure function (no Tk / no IO): unit-tested headlessly."""
    s = int(max(0, round(float(seconds))))
    days, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    if days:
        return f"{days}d {h:02d}:{m:02d}:{sec:02d}"
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _fmt_local(epoch) -> str:
    """Render an epoch as a local 'Mon DD HH:MM:SS' string (or '?')."""
    if not epoch:
        return "?"
    try:
        return datetime.fromtimestamp(epoch).strftime("%b %d %H:%M:%S")
    except Exception:
        return "?"


def derive_view(snap: dict, now: float) -> dict:
    """Pure state-derivation: (status snapshot, current epoch) -> display model.

    Returns a dict with: headline (WATCHING/PENDING/FIRING/DONE/HELD), countdown
    (HH:MM:SS string or None), detail (kind/reset/fire line or ''), status_line
    (one-line human status), remaining (seconds float or None). No Tk, no IO ->
    unit-tested headlessly. HELD (kill-switch) overrides the headline but the
    countdown still renders so the owner sees what is being held and when."""
    state = snap.get("state", "WATCHING")
    stopped = bool(snap.get("stopped"))
    fire_at = snap.get("fire_at")
    kind = snap.get("kind")
    reset_str = snap.get("reset_str")
    reset_epoch = snap.get("reset_epoch")

    headline = "HELD" if stopped else state

    remaining = None
    countdown = None
    detail = ""
    if state == "PENDING" and fire_at is not None:
        remaining = max(0.0, float(fire_at) - float(now))
        countdown = format_hms(remaining)
        detail = (f"{kind or '?'}  ·  resets {reset_str or '?'}  ·  "
                  f"reset {_fmt_local(reset_epoch)}  ·  fire {_fmt_local(fire_at)}")

    if stopped:
        if state == "PENDING":
            status = "HELD by kill-switch — paused; will inject when resumed"
        else:
            status = "HELD by kill-switch — injection paused"
    elif state == "PENDING":
        status = "Limit hit — counting down to automated resume"
    elif state == "FIRING":
        status = "Injecting resume message…"
    elif state == "DONE":
        status = "Injected. Watching for the next limit."
    else:  # WATCHING
        status = "Watching — no limit hit yet"

    return {
        "headline": headline,
        "countdown": countdown,
        "remaining": remaining,
        "detail": detail,
        "status_line": status,
        "kind": kind,
        "reset_str": reset_str,
    }


# Headline -> accent colour (dark theme).
_STATE_COLORS = {
    "WATCHING": "#7fbf7f",
    "PENDING": "#f0c040",
    "FIRING": "#e06666",
    "DONE": "#7fbf7f",
    "HELD": "#e0a020",
}
_BG = "#1e1e1e"
_FG = "#d4d4d4"
_SUBFG = "#9a9a9a"


class StatusWindow:
    """Always-on-top Tk status window rendering a WatchStatus snapshot.

    tick_ms=0 disables self-scheduling so tests can drive refresh() manually.
    Buttons route through the shared request flags / the stop-file -- the actual
    injection stays the watch loop's single guarded do_inject path."""

    def __init__(self, root, shared: WatchStatus, stop_file: str, tick_ms: int = 1000):
        self.root = root
        self.shared = shared
        self.stop_file = stop_file
        self.tick_ms = tick_ms
        self._scheduled = None

        root.title("autoresume — status")
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        root.configure(bg=_BG)
        root.resizable(False, False)

        big = tkfont.Font(family="Consolas", size=30, weight="bold") if tkfont else None
        state_f = tkfont.Font(family="Segoe UI", size=13, weight="bold") if tkfont else None
        small = tkfont.Font(family="Segoe UI", size=9) if tkfont else None
        mono = tkfont.Font(family="Consolas", size=8) if tkfont else None

        self.var_state = tk.StringVar(value="WATCHING")
        self.var_countdown = tk.StringVar(value="--:--:--")
        self.var_status = tk.StringVar(value="Watching — no limit hit yet")
        self.var_detail = tk.StringVar(value="")
        self.var_lastlog = tk.StringVar(value="")
        self.var_pause = tk.StringVar(value="Pause")

        pad = {"padx": 12}
        self.lbl_state = tk.Label(root, textvariable=self.var_state, font=state_f,
                                  bg=_BG, fg=_STATE_COLORS["WATCHING"], anchor="w")
        self.lbl_state.pack(fill="x", pady=(10, 0), **pad)

        self.lbl_countdown = tk.Label(root, textvariable=self.var_countdown, font=big,
                                      bg=_BG, fg=_FG, anchor="w")
        self.lbl_countdown.pack(fill="x", **pad)

        tk.Label(root, textvariable=self.var_status, font=small, bg=_BG, fg=_SUBFG,
                 anchor="w", justify="left", wraplength=380).pack(fill="x", **pad)
        tk.Label(root, textvariable=self.var_detail, font=small, bg=_BG, fg=_SUBFG,
                 anchor="w", justify="left", wraplength=380).pack(fill="x", pady=(2, 6), **pad)

        btns = tk.Frame(root, bg=_BG)
        btns.pack(fill="x", pady=(0, 6), **pad)
        self.btn_pause = tk.Button(btns, textvariable=self.var_pause, width=9,
                                   command=self.toggle_pause)
        self.btn_pause.pack(side="left")
        tk.Button(btns, text="Cancel reset", width=11,
                  command=self.cancel_reset).pack(side="left", padx=(6, 0))
        tk.Button(btns, text="Inject now", width=10,
                  command=self.inject_now).pack(side="left", padx=(6, 0))

        tk.Label(root, textvariable=self.var_lastlog, font=mono, bg=_BG, fg="#6a6a6a",
                 anchor="w", justify="left", wraplength=380).pack(fill="x",
                                                                  pady=(0, 8), **pad)

    # -- rendering --------------------------------------------------------- #
    def refresh(self):
        snap = self.shared.snapshot()
        # The stop-file is the source of truth for the kill switch; check it live
        # so Pause/Resume reflects INSTANTLY (not at the next 5s watch poll).
        snap["stopped"] = bool(self.stop_file and os.path.exists(self.stop_file))
        view = derive_view(snap, time.time())

        self.var_state.set(view["headline"])
        self.lbl_state.configure(fg=_STATE_COLORS.get(view["headline"], _FG))
        self.var_countdown.set(view["countdown"] or "--:--:--")
        self.var_status.set(view["status_line"])
        self.var_detail.set(view["detail"])
        last = snap.get("last_log") or ""
        self.var_lastlog.set(last if len(last) <= 90 else last[:87] + "…")
        self.var_pause.set("Resume" if snap["stopped"] else "Pause")

        if self.tick_ms:
            self._scheduled = self.root.after(self.tick_ms, self.refresh)

    def start(self):
        self.refresh()

    # -- button handlers --------------------------------------------------- #
    def toggle_pause(self):
        """Toggle the kill-switch stop-file (the existing pause mechanism)."""
        try:
            if os.path.exists(self.stop_file):
                os.remove(self.stop_file)
            else:
                d = os.path.dirname(self.stop_file)
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(self.stop_file, "w", encoding="utf-8") as fh:
                    fh.write("autoresume paused via GUI\n")
        except OSError:
            pass
        # reflect immediately
        self.var_pause.set("Resume" if os.path.exists(self.stop_file) else "Pause")

    def cancel_reset(self):
        self.shared.request_cancel()

    def inject_now(self):
        self.shared.request_inject_now()


def _watch_thread(args, shared: WatchStatus):
    """Run the watch loop for the GUI, surfacing a fatal crash into the status."""
    try:
        run_watch(args, shared=shared)
    except Exception as e:  # noqa: BLE001 - keep the window informative on crash
        try:
            log_event(f"WATCH-THREAD CRASHED: {e!r}", args.log_file)
        except Exception:
            pass
        shared.update(state="DONE", last_action=f"watch loop crashed: {e!r}")


def run_gui(args):
    """Start the watch loop in a background thread and show the status window."""
    if tk is None:
        log_event("GUI unavailable (tkinter not importable); running headless watch",
                  args.log_file)
        return run_watch(args)

    shared = WatchStatus()
    t = threading.Thread(target=_watch_thread, args=(args, shared),
                         name="autoresume-watch", daemon=True)
    t.start()

    root = tk.Tk()
    win = StatusWindow(root, shared, args.stop_file, tick_ms=1000)
    win.start()
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_install_shortcut(args):
    """Best-effort: drop a Desktop shortcut (.lnk) to the .vbs launcher.

    Uses a transient WScript snippet (Windows-native, no third-party deps). Never
    fails the process if shortcut creation is unavailable -- prints and returns."""
    here = os.path.dirname(os.path.abspath(__file__))
    launcher = os.path.join(here, "autoresume-gui.vbs")
    if not os.path.isfile(launcher):
        print(f"launcher not found: {launcher}")
        return 1
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    lnk = os.path.join(desktop, "autoresume status.lnk")
    icon = os.environ.get("SystemRoot", r"C:\Windows") + r"\System32\wscript.exe"
    vbs = (
        'Set s = CreateObject("WScript.Shell")\r\n'
        f'Set lnk = s.CreateShortcut("{lnk}")\r\n'
        f'lnk.TargetPath = "{launcher}"\r\n'
        f'lnk.WorkingDirectory = "{here}"\r\n'
        'lnk.WindowStyle = 7\r\n'
        f'lnk.IconLocation = "{icon},0"\r\n'
        'lnk.Description = "autoresume status / countdown window"\r\n'
        'lnk.Save\r\n'
    )
    import subprocess
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), "autoresume_mkshortcut.vbs")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(vbs)
        rc = subprocess.call(["cscript", "//nologo", "//b", tmp])
        if rc == 0 and os.path.exists(lnk):
            print(f"created Desktop shortcut: {lnk}")
            return 0
        print(f"shortcut creation returned rc={rc}; lnk exists={os.path.exists(lnk)}")
        return 0  # best-effort: never hard-fail
    except Exception as e:  # noqa: BLE001
        print(f"shortcut creation unavailable ({e!r}); skipping (best-effort)")
        return 0
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Subcommands: parse a file, self-test, inject-now                             #
# --------------------------------------------------------------------------- #


def cmd_parse_file(args):
    """Parse every limit-hit line in a JSONL file and print KIND + resolved
    reset. Used for testing against fixtures."""
    n = 0
    with open(args.file, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = extract_limit_text(obj)
            if not text:
                continue
            parsed = parse_limit_text(text)
            n += 1
            if parsed["kind"] == "monthly spend":
                print(f"[{n}] kind=monthly spend reset=<none: billing cap> "
                      f"raw={parsed['text']!r}")
                continue
            try:
                epoch, dt = resolve_reset_epoch(parsed["reset_str"], now=args.now)
                local = datetime.fromtimestamp(epoch)
                print(f"[{n}] kind={parsed['kind']} reset_str={parsed['reset_str']!r} "
                      f"resolved_tz={dt.isoformat()} resolved_local={local.isoformat()} "
                      f"epoch={int(epoch)}")
            except Exception as e:  # noqa: BLE001
                print(f"[{n}] kind={parsed['kind']} reset_str={parsed['reset_str']!r} "
                      f"RESOLVE-ERROR {e}")
    if n == 0:
        print("no limit-hit lines found")


def cmd_inject_now(args):
    """Inject an arbitrary message into a target window RIGHT NOW (for testing
    the injection path, e.g. into Notepad)."""
    log = lambda m: log_event(m, args.log_file)
    if args.injector == "ahk":
        ok, detail = inject_via_ahk(args.message, args.ahk_exe, args.ahk_script, log)
    else:
        ok, detail = inject_message(
            args.message, args.target_proc, args.target_title,
            args.prefer_title, press_enter=not args.no_enter,
            focus_method=args.focus_method, log=log)
    print(f"inject ok={ok} detail={detail}")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def build_argparser():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    def add_common(sp):
        sp.add_argument("--watch-dir", default=DEFAULT_WATCH_DIR)
        sp.add_argument("--target-proc", default=TARGET_PROC)
        sp.add_argument("--target-title", default=TARGET_TITLE)
        sp.add_argument("--prefer-title", default=PREFER_TITLE)
        sp.add_argument("--buffer", type=int, default=BUFFER_SECONDS)
        sp.add_argument("--poll", type=int, default=POLL_INTERVAL)
        sp.add_argument("--min-interval", type=int, default=MIN_INJECT_INTERVAL)
        sp.add_argument("--stale-grace", type=int, default=STALE_RESET_GRACE)
        sp.add_argument("--state-file", default=STATE_FILE)
        sp.add_argument("--log-file", default=LOG_FILE)
        sp.add_argument("--stop-file", default=STOP_FILE)
        sp.add_argument("--injector", choices=["win32", "ahk"], default="win32")
        sp.add_argument("--focus-method", choices=["keybind", "palette", "none"],
                        default=FOCUS_METHOD,
                        help="how to focus the Claude Code chat input before "
                             "typing (keybind=ctrl+alt+shift+k -> "
                             "claude-vscode.focus; palette=Command Palette)")
        sp.add_argument("--ahk-exe",
                        default=r"C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe")
        sp.add_argument("--ahk-script",
                        default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                             "inject.ahk"))

    w = sub.add_parser("watch", help="watch transcript and auto-resume on reset")
    add_common(w)
    w.add_argument("--from-start", action="store_true",
                   help="parse the transcript from the beginning (default: tail from EOF)")
    w.add_argument("--dry-run", action="store_true",
                   help="detect+log but never type into any window")
    w.add_argument("--gui", action="store_true",
                   help="show the always-on-top status/countdown window "
                        "(additive view over the same watch loop)")

    sc = sub.add_parser("install-shortcut",
                        help="best-effort: drop a Desktop shortcut to the GUI launcher")

    pf = sub.add_parser("parse-file", help="parse a JSONL file's limit-hit lines (test)")
    pf.add_argument("file")
    pf.add_argument("--now", type=lambda s: datetime.fromisoformat(s),
                    default=None, help="ISO time to resolve relative to (for tests)")

    inj = sub.add_parser("inject-now", help="inject a message into a window now (test)")
    add_common(inj)
    inj.add_argument("message")
    inj.add_argument("--no-enter", action="store_true")

    return p


def main(argv=None):
    args = build_argparser().parse_args(argv)
    if args.cmd == "watch":
        if getattr(args, "gui", False):
            return run_gui(args)
        try:
            run_watch(args)
        except KeyboardInterrupt:
            print("\nstopped.")
        return 0
    if args.cmd == "install-shortcut":
        return cmd_install_shortcut(args)
    if args.cmd == "parse-file":
        cmd_parse_file(args)
        return 0
    if args.cmd == "inject-now":
        return cmd_inject_now(args)
    build_argparser().print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
