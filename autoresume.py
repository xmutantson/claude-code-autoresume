#!/usr/bin/env python3
"""
autoresume.py -- Claude Code auto-resume on usage-limit reset (Windows).

Detects when a Claude Code session has hit a usage limit, figures out WHEN that
limit resets, waits until the reset (+ a small buffer), then types a
plain-language "automated resume" message into the focused Claude Code chat
input in VS Code and presses Enter -- so the autonomous session picks up where
it left off without a human present.

PRIMARY detection (self-sufficient): poll the authoritative usage endpoint
    GET https://api.anthropic.com/api/oauth/usage
(the same source the Usage Monitor for Claude uses; approach adapted from
jens-duttke/usage-monitor-for-claude, MIT). A quota is blocked when its
percent/utilization is >= 100; the resets_at timestamp gives the exact reset.
autoresume polls the endpoint DIRECTLY (it does NOT depend on the Usage Monitor
app) on an ADAPTIVE cadence so it doesn't trip the endpoint's PER-OAUTH-TOKEN
rate limit -- the token is shared with the Usage Monitor, and two fast pollers on
one token tripped HTTP 429 for both. IDLE it polls slowly (~180s, --poll-interval)
just to catch a fresh hit; once ARMED (a hit observed, resets_at known) it SLEEPS
to near the known reset -- the account is definitely still blocked until then --
then polls fast to catch the exact clear (~10-50x fewer requests than a fixed
fast cadence, same reliability). On HTTP 429 it honors Retry-After in full with an
exponential backoff, and the backoff window suppresses BOTH the idle and armed
polls so it never piles onto a rate-limited endpoint. All credential + network
code is isolated in usage_api.py; the OAuth token is never logged. See --source.

ARM-ON-HIT / FIRE-ON-RESET state machine: the watcher polls continuously. When a
focused limit window is OBSERVED at utilization >= 100% (a real HIT) it ARMS and
persists {armed, window, resets_at} to the state file. While ARMED it keeps
polling and FIRES the resume the moment the armed window CLEARS (utilization drops
back below the block line) OR the reset time is reached -- then DISARMS. It fires
ONLY if a hit was actually observed: a window sitting at 0% with no prior observed
hit NEVER fires. The armed state survives a watcher restart (reloaded from the
state file), so a reset that lands while the watcher is briefly down is still
picked up and resumed on the next poll.

MANUAL mode: the owner can set an explicit resume time -- in the GUI (a "resume at
HH:MM" field + Set/Clear, with a manual-only toggle) or via --resume-at
(HH:MM | +Nm | ISO). The watcher then fires at that time regardless of API
detection (useful when an error message already states the reset time, e.g.
"resets 3:10pm"). Auto-detection stays on as a backstop unless manual-only is set.
The GUI and the CLI share one manual-request file (same filesystem-IPC pattern as
the kill-switch stop-file), so a manual time is honoured headless and across
restarts.

FALLBACK detection: the legacy transcript watcher (watch the session JSONL for a
limit-hit line). Kept for machines with no readable credentials; it is no longer
the default and its transcript-text matching is only used in --source transcript.

Replaces a fixed-time blind-Enter script (the classic "send {Enter} at 1:15am"
AutoHotkey hack) with limit-aware detection, focused-window targeting,
send-once-per-reset dedup, a kill switch, and full logging.

Design: detection/schedule/dedup in Python (stdlib: json + datetime + urllib +
zoneinfo/tzdata); injection via pure Win32 SendInput (no third-party deps). An
AHK v2 fallback injector (inject.ahk) is provided as an alternative.

No external Python packages are required beyond the standard library plus
`tzdata` (already present) for zoneinfo on Windows.
"""

from __future__ import annotations

__version__ = "0.5.0"   # 0.5.0: fix the shared-token HTTP 429 + a stuck-arm bug.
                        #        ADAPTIVE polling: idle ~180s; while ARMED sleep to
                        #        near the known reset then poll fast (~10-50x fewer
                        #        requests). Real 429 backoff: honor Retry-After in
                        #        FULL + exponential ramp (was capped at 120s), and
                        #        a live backoff suppresses BOTH idle + armed polls.
                        #        RE-ARM a still-blocked, future-reset quota even if
                        #        it was marked handled (a prior inject-now/premature
                        #        fire before the actual reset no longer disarms auto
                        #        for that reset). Manual resume time now shows a
                        #        COUNTDOWN in the GUI, not just a status line;
                        # 0.4.0: URI injector (DEFAULT) -- target the EXACT Claude
                        #        Code session tab by id via the
                        #        vscode://anthropic.claude-code/open?session=<ID>
                        #        &prompt=<URLENCODED> deep link (focuses that tab +
                        #        pre-fills the prompt; one Enter submits). Replaces
                        #        the "focus a VS Code window + guess the tab"
                        #        keystroke path as the default; --injector win32
                        #        (old keystroke) + ahk remain as fallbacks;
                        # 0.3.2: stop un-fullscreening VSCode on inject (only
                        #        un-minimize, never SW_RESTORE a maximized window);
                        # 0.3.1: fix Inject-now button doing nothing in plain
                        #        watching mode (fire a manual inject with no pending);
                        # 0.3.0: fast direct polling (no monitor backoff),
                        #        arm-on-hit/fire-on-clear state machine (persisted
                        #        across restarts), GUI + CLI manual resume time

import argparse
import ctypes
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import usage_api

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

BUFFER_SECONDS = 45          # fire at reset + this (resets_at is exact, but the
                             # server clock can lag applying the reset). +30-60s.
POLL_INTERVAL = 5            # loop tick (s): kill-switch / GUI responsiveness. The
                             # usage-API source rate-limits its OWN network calls
                             # independently (see below); this is not the API rate.
MIN_INJECT_INTERVAL = 60     # global backstop: >=60s between any two injections
STALE_RESET_GRACE = 6 * 3600 # if a reset already passed by more than this, treat
                             # the entry as historical and do NOT inject.

# --- usage-API detection cadence (seconds) --------------------------------- #
# ADAPTIVE POLLING. The usage endpoint rate-limits PER OAUTH TOKEN, and the token
# is shared with the Usage Monitor app -- so a fixed fast cadence (the old 30s)
# stacked on the monitor's polling tripped HTTP 429 for BOTH tools. We now poll
# lightly:
#   * IDLE (hunting for a hit): every IDLE_POLL_INTERVAL. A hit is caught within
#     one idle poll, then armed.
#   * ARMED (a hit observed, resets_at known): we do NOT keep polling the whole
#     way to the reset -- the account is definitely still blocked until then. We
#     SLEEP until near the known reset (a slow ARMED_DRIFT_RECHECK safety check in
#     case resets_at moved / cleared early), then poll fast (min(idle,
#     USAGE_POLL_INTERVAL)) to catch the exact clear. ~10-50x fewer requests than
#     the old fixed 30s, same reliability.
# The arm-on-hit / fire-on-clear machine (below) still catches the reset within
# one (now near-reset, fast) poll.
IDLE_POLL_INTERVAL = 180     # idle usage-endpoint poll cadence (s); --poll-interval.
USAGE_POLL_INTERVAL = 30     # FAST-confirm cap near/after the reset (also the
                             # legacy --usage-poll default). While armed we never
                             # confirm faster than this.
ARMED_DRIFT_RECHECK = 600    # while ARMED but far from the reset, re-confirm at
                             # most this often (safety net for a moved/early reset)
RL_BACKOFF_CAP = 900         # max seconds to back off after repeated HTTP 429s
FUTURE_REARM_GRACE = 120     # re-arm a "handled" quota only if its reset is still
                             # at least this many seconds in the FUTURE
MONITOR_POLL_INTERVAL = 900  # DEPRECATED / ignored -- kept only so an existing
                             # --monitor-poll argument still parses. Cadence no
                             # longer backs off for the monitor.
CONFIRM_INTERVAL = 30        # while ARMED past the reset time but the server has
                             # not applied the reset yet, re-poll every this-many-s
CONFIRM_BELOW = 90.0         # a quota's utilization must drop below this to count
                             # as CLEARED (a real reset drops the window to ~0, so
                             # 90 is an unambiguous "cleared" line under the 100
                             # block threshold)
MONITOR_CACHE_TTL = 60       # cache the tasklist monitor-presence check this long
                             # (only used for an informational log now)
USAGE_MONITOR_PROC = "UsageMonitorForClaude.exe"  # the Usage Monitor app image

_LOCALAPPDATA = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
STATE_DIR = os.path.join(_LOCALAPPDATA, "claude-autoresume")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LOG_FILE = os.path.join(STATE_DIR, "autoresume.log")
_TEMP_DIR = os.environ.get("TEMP", STATE_DIR)
STOP_FILE = os.path.join(_TEMP_DIR, "autoresume.stop")
# Manual-resume-time request file (GUI + CLI shared IPC, same filesystem pattern
# as the kill-switch stop-file): JSON {"resume_at": <epoch>, "manual_only": bool}.
MANUAL_FILE = os.path.join(_TEMP_DIR, "autoresume.manual.json")

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
# shell32 for ShellExecuteW: fire the vscode:// deep link that focuses the exact
# Claude Code session tab (the URI injector, the v0.4.0 default). Guarded import
# so the module still loads (parse/inject unit tests) if shell32 is unavailable.
try:
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
except Exception:  # pragma: no cover - non-Windows / no shell32
    shell32 = None  # type: ignore

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
SW_SHOWNORMAL = 1       # ShellExecuteW nCmdShow for the vscode:// deep link
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
user32.IsIconic.argtypes = (wintypes.HWND,)
user32.IsIconic.restype = wintypes.BOOL
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

if shell32 is not None:
    # HINSTANCE ShellExecuteW(hwnd, lpOperation, lpFile, lpParameters,
    #                         lpDirectory, nShowCmd). Returns a value > 32 on
    # success; <= 32 is an error code. HINSTANCE == c_void_p (int or None).
    shell32.ShellExecuteW.argtypes = (
        wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.LPCWSTR, ctypes.c_int,
    )
    shell32.ShellExecuteW.restype = wintypes.HINSTANCE

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


def _enum_target_windows(proc_name=TARGET_PROC, title_substr=TARGET_TITLE):
    """Return an ordered list of (hwnd, title) for visible windows belonging to
    `proc_name` whose title contains `title_substr`. Order is EnumWindows Z-order
    (top-most / most-recently-active first)."""
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
    return matches


def find_target_window(proc_name=TARGET_PROC, title_substr=TARGET_TITLE,
                       prefer_substr=PREFER_TITLE):
    """Return (hwnd, title) of a matching window, preferring `prefer_substr`, else
    the first in Z-order. (None, None) if nothing matches. Kept for API stability;
    the watcher now uses select_target_window (foreground-first)."""
    matches = _enum_target_windows(proc_name, title_substr)
    if not matches:
        return None, None
    if prefer_substr:
        for hwnd, title in matches:
            if prefer_substr.lower() in title.lower():
                return hwnd, title
    return matches[0]


def select_target_window(proc_name=TARGET_PROC, title_substr=TARGET_TITLE,
                         prefer_substr=PREFER_TITLE, log=None):
    """Pick the injection target FOREGROUND-FIRST (owner: "whichever is currently
    in focus"). Order of preference:

      1. `prefer_substr` override -- if set and a window's title matches, use it.
      2. The CURRENT foreground window, if it is one of the matching VS Code
         windows (right process + title). This is the common case: type into the
         window the owner left focused.
      3. If exactly one matching VS Code window exists, use it.
      4. Otherwise best-effort: the most-recently-active (first in Z-order)
         matching window, with the ambiguity LOGGED.

    Returns (hwnd, title, reason); (None, None, 'no-target-window') if nothing
    matches. The caller still runs the foreground guard before typing."""
    def _log(m):
        if log:
            log(m)

    matches = _enum_target_windows(proc_name, title_substr)
    if not matches:
        return None, None, "no-target-window"

    if prefer_substr:
        for hwnd, title in matches:
            if prefer_substr.lower() in title.lower():
                return hwnd, title, "prefer-title override"

    fg = user32.GetForegroundWindow()
    if fg:
        for hwnd, title in matches:
            if hwnd == fg:
                return hwnd, title, "current foreground window"

    if len(matches) == 1:
        return matches[0][0], matches[0][1], "sole VS Code window"

    hwnd, title = matches[0]
    _log(f"AMBIGUOUS target: {len(matches)} VS Code windows and none is in "
         f"focus; best-effort using most-recent {title!r}")
    return hwnd, title, "best-effort most-recent (ambiguous)"


def usage_monitor_running(proc_name=USAGE_MONITOR_PROC) -> bool:
    """True iff a process image named `proc_name` (the Usage Monitor for Claude
    app) is running. psutil-free: uses `tasklist` on Windows, returns False
    elsewhere. Best-effort -- any failure reads as 'not running'."""
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {proc_name}", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:  # noqa: BLE001 - process probe is best-effort
        return False
    return proc_name.lower() in (out.stdout or "").lower()


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

    # Only un-MINIMIZE; never SW_RESTORE a maximized/fullscreen window -- that
    # would un-fullscreen VSCode (the owner's complaint). BringWindowToTop +
    # SetForegroundWindow bring it forward without changing the maximize state.
    if user32.IsIconic(hwnd):
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

    hwnd, title, reason = select_target_window(proc_name, title_substr,
                                               prefer_substr, log=log)
    if not hwnd:
        _log(f"ABORT inject: no target window (proc={proc_name} "
             f"title~{title_substr!r})")
        return False, "no-target-window"
    _log(f"TARGET window {title!r} ({reason})")

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
# URI injector (v0.4.0 DEFAULT): target the EXACT session tab by id            #
#                                                                              #
# The keystroke injectors above focus a VS Code WINDOW and type into whatever  #
# Claude tab happens to be active -- but one VS Code window can hold MULTIPLE   #
# Claude session tabs, so the keystrokes can land in the WRONG conversation.   #
# The Claude Code extension exposes a deep link that targets a session by id:  #
#                                                                              #
#   vscode://anthropic.claude-code/open?session=<ID>&prompt=<URLENCODED>       #
#                                                                              #
# session=<ID> FOCUSES that exact tab (opening it in the focused window's       #
# workspace if not already open); prompt= PRE-FILLS the chat input -- it is NOT #
# auto-submitted, so we send exactly ONE Enter to submit. This makes injection  #
# tab-precise instead of "a window + a guessed tab".                            #
# Docs: https://code.claude.com/docs/en/vs-code#launch-a-vs-code-tab-from-      #
#       other-tools                                                             #
#                                                                              #
# CAVEATS (documented + handled below):                                        #
#  * The pre-fill is NOT submitted by the URI -> the single guarded Enter       #
#    submits it. If a confirmation ("open external website?" / "switch apps?")  #
#    intercepts the first Enter, pass --uri-extra-enter to send a second one,   #
#    or (recommended) check "always allow" once so no dialog appears again.     #
#  * The session id must belong to the FOCUSED window's workspace, else the     #
#    extension starts a FRESH conversation -> window targeting still matters,   #
#    so we foreground the right VS Code window BEFORE firing the URI.           #
#  * Requires a recent extension build that understands the `session` param. On #
#    an old build the URI is a no-op (nothing pre-fills); the log records the   #
#    fire so a silent no-op is diagnosable.                                     #
# --------------------------------------------------------------------------- #

CLAUDE_URI_BASE = "vscode://anthropic.claude-code/open"


def build_resume_uri(session_id: str, message: str) -> str:
    """Build the Claude Code deep link that focuses session `session_id` and
    pre-fills `message`. session + prompt are URL-encoded with quote(safe="")
    so every reserved char (including '/', '&', '=', spaces, the middle-dot in
    the reset string) is percent-escaped."""
    return (f"{CLAUDE_URI_BASE}?session=" + quote(session_id or "", safe="")
            + "&prompt=" + quote(message or "", safe=""))


def _shell_execute(url: str) -> int:
    """Fire `url` via ShellExecuteW(open). Returns the HINSTANCE-ish code
    (> 32 = success, <= 32 = error). Does NOT go through a shell string, so the
    URL is passed verbatim to the vscode:// protocol handler."""
    if shell32 is None:
        return 0
    res = shell32.ShellExecuteW(None, "open", url, None, None, SW_SHOWNORMAL)
    if res is None:
        return 0
    try:
        return int(res)
    except (TypeError, ValueError):
        return 0


def inject_via_uri(message: str, session_id: str, workspace_title: str,
                   proc_name=TARGET_PROC, title_substr=TARGET_TITLE,
                   prefer_substr=None, press_enter=True, settle=0.35,
                   uri_settle=0.5, extra_enter=False,
                   shell_execute=None, log=None):
    """Inject `message` by targeting the EXACT Claude Code session tab.

    Steps:
      1. Foreground the correct VS Code window (reuse select_target_window /
         activate_window; prefer_substr defaults to `workspace_title`, the
         workspace folder derived from the session's cwd). With a single window
         this is trivial, but it keeps the session opening in the right
         workspace when several windows exist.
      2. Fire vscode://anthropic.claude-code/open?session=<id>&prompt=<msg> via
         ShellExecuteW (NOT a shell string). The extension focuses that exact
         tab and pre-fills the prompt.
      3. Settle ~uri_settle s for the tab to focus + pre-fill.
      4. Send ONE Enter to submit -- reusing the foreground guard so it never
         Enters into a non-Claude window.

    `shell_execute` is injectable for tests (defaults to the real ShellExecuteW).
    Returns (ok, detail)."""
    def _log(m):
        if log:
            log(m)

    fire = shell_execute if shell_execute is not None else _shell_execute

    if not session_id:
        _log("ABORT inject(uri): no session id derivable; cannot target a "
             "session tab (fall back to --injector win32)")
        return False, "no-session-id"

    url = build_resume_uri(session_id, message)

    # 1) Foreground the correct VS Code window so the URI opens the session in
    #    THAT window's workspace (a session id from another workspace would start
    #    a fresh chat). prefer the workspace folder as the disambiguator.
    prefer = prefer_substr if prefer_substr is not None else (
        workspace_title or PREFER_TITLE)
    hwnd, title, reason = select_target_window(proc_name, title_substr, prefer,
                                               log=log)
    fg_ok = False
    if hwnd:
        _log(f"TARGET window {title!r} ({reason})")
        fg_ok, fg_title = foreground_matches(proc_name, title_substr)
        if not fg_ok:
            activate_window(hwnd)
            time.sleep(settle)
            fg_ok, fg_title = foreground_matches(proc_name, title_substr)
        if not fg_ok:
            _log(f"WARN inject(uri): foreground guard failed (fg={fg_title!r}); "
                 f"firing the URI anyway (session=<id> targets the exact tab, but "
                 f"if the focused workspace differs the extension may open a fresh "
                 f"chat)")
    else:
        _log(f"WARN inject(uri): no VS Code window matched (proc={proc_name} "
             f"title~{title_substr!r}); firing the URI to let the OS route it")

    # 2) Fire the deep link.
    rc = fire(url)
    if rc <= 32:
        _log(f"ABORT inject(uri): ShellExecuteW failed rc={rc} for session="
             f"{session_id}")
        return False, f"shellexecute-rc-{rc}"
    _log(f"URI fired: session={session_id} workspace={workspace_title!r} "
         f"prompt={len(message)} chars rc={rc}")

    # 3) Let the tab focus + the prompt pre-fill settle.
    time.sleep(uri_settle)

    if not press_enter:
        _log("inject(uri): prompt PRE-FILLED, Enter suppressed (--no-enter / "
             "test); submit manually")
        return True, f"prefilled:{workspace_title or 'uri'}"

    # 4) Submit with ONE Enter -- guarded so we never Enter into a non-VS-Code
    #    window (the URI may have popped a confirmation that stole focus, or the
    #    handler brought a different app forward).
    ok, fg_title = foreground_matches(proc_name, title_substr)
    if not ok and hwnd:
        activate_window(hwnd)
        time.sleep(0.2)
        ok, fg_title = foreground_matches(proc_name, title_substr)
    if not ok:
        _log(f"HOLD inject(uri) Enter: foreground is {fg_title!r}, not VS Code; "
             f"the URI pre-filled the prompt but NOT submitting (refusing Enter "
             f"into the wrong window). Submit manually, or use --injector win32.")
        return True, f"prefilled-not-submitted:{fg_title!r}"
    time.sleep(0.05)
    press_vk(VK_RETURN)
    if extra_enter:
        # Environments where a one-time "open external app?" confirmation eats
        # the first Enter: a second Enter (after a short settle) submits the now
        # pre-filled prompt. Harmless when no dialog appeared (an empty second
        # submit is a no-op in the Claude input).
        time.sleep(uri_settle)
        press_vk(VK_RETURN)
    _log(f"INJECTED via URI into {fg_title!r}: session={session_id} submitted "
         f"with {'two Enters' if extra_enter else 'one Enter'}")
    return True, fg_title


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
# Manual resume time: parsing + shared request file (GUI + CLI IPC)            #
#                                                                              #
# The manual-request file is the counterpart to the kill-switch stop-file: a   #
# tiny JSON the GUI (or --resume-at at startup) writes and the watch loop reads #
# every tick. Filesystem IPC (not an in-process flag) so a manual time is       #
# honoured headless, survives a restart, and works if the GUI and a headless    #
# watcher are separate processes -- exactly how pause/stop already works.       #
# --------------------------------------------------------------------------- #

_RESUME_REL_RE = re.compile(r"^\+\s*(\d+)\s*([smh]?)$", re.IGNORECASE)
_RESUME_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_resume_at(spec: str, now: datetime | None = None) -> float:
    """Resolve a manual resume-time spec to an epoch (seconds).

    Accepts:
      - '+Nm' / '+Nh' / '+Ns' / '+N'  -> now + N minutes (default unit minutes),
        hours ('h') or seconds ('s').
      - 'HH:MM'                        -> the NEXT future occurrence of that local
        wall-clock time (today, or tomorrow if already passed).
      - an ISO-8601 timestamp          -> that instant (naive is taken as local).

    Raises ValueError on an unparseable spec. `now` is injectable for tests."""
    if spec is None:
        raise ValueError("no resume-at spec")
    s = str(spec).strip()
    if not s:
        raise ValueError("empty resume-at spec")
    base = datetime.now().astimezone() if now is None else now
    if base.tzinfo is None:
        base = base.astimezone()

    m = _RESUME_REL_RE.match(s)
    if m:
        n = int(m.group(1))
        unit = (m.group(2) or "m").lower()
        mult = {"s": 1, "m": 60, "h": 3600}[unit]
        return (base + timedelta(seconds=n * mult)).timestamp()

    m = _RESUME_HHMM_RE.match(s)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"HH:MM out of range: {s!r}")
        dt = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt <= base:
            dt = dt + timedelta(days=1)
        return dt.timestamp()

    # ISO-8601 fallback.
    try:
        iso = s[:-1] + "+00:00" if s.endswith("Z") else s
        dt = datetime.fromisoformat(iso)
    except ValueError:
        raise ValueError(f"unrecognised resume-at {s!r} "
                         f"(use HH:MM, +Nm, or an ISO timestamp)") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=base.tzinfo)
    return dt.timestamp()


def read_manual_request(path=MANUAL_FILE):
    """Return {'resume_at': float, 'manual_only': bool} from the manual-request
    file, or None if it is absent / malformed / missing a resume_at."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    ra = data.get("resume_at")
    try:
        ra = float(ra)
    except (TypeError, ValueError):
        return None
    return {"resume_at": ra, "manual_only": bool(data.get("manual_only", False))}


def write_manual_request(path, resume_at: float, manual_only: bool = False):
    """Atomically write the manual-request file (GUI Set / CLI --resume-at)."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"resume_at": float(resume_at),
                   "manual_only": bool(manual_only)}, fh)
    os.replace(tmp, path)


def clear_manual_request(path=MANUAL_FILE):
    """Remove the manual-request file (GUI Clear / after a manual fire)."""
    try:
        os.remove(path)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Arm persistence: survive a watcher restart mid-arm                           #
#                                                                              #
# When a hit is OBSERVED the loop persists the armed pending into state.json so #
# a restart resumes it -- and a reset that lands while the watcher was briefly  #
# down still fires on the next poll. Only the label is kept in `meta` (JSON     #
# safe; confirm_reset needs nothing else), never a datetime.                    #
# --------------------------------------------------------------------------- #

# How stale a persisted arm may be (seconds) before it is treated as historical
# on restore and dropped without firing (a clearly dead session, not a missed
# reset). Generous: a reset missed during a short downtime still fires.
ARM_RESTORE_MAX_STALE = 3 * 86400


def save_arm(state: dict, pending: dict, state_file=STATE_FILE):
    """Persist the armed pending (JSON-safe) into state and write it."""
    state["arm"] = {
        "kind": pending.get("kind"),
        "reset_str": pending.get("reset_str"),
        "reset_epoch": pending.get("reset_epoch"),
        "key": pending.get("key"),
        "fire_at": pending.get("fire_at"),
        "meta": {"label": (pending.get("meta") or {}).get("label")
                 or pending.get("kind")},
        "observed_at": pending.get("observed_at", time.time()),
    }
    save_state(state, state_file)


def clear_arm(state: dict, state_file=STATE_FILE):
    if state.get("arm") is not None:
        state["arm"] = None
        save_state(state, state_file)


def restore_arm(state: dict, now=None):
    """Reconstruct an in-memory pending from a persisted arm, or None.

    Drops the arm if it is already handled (fired) or absurdly stale."""
    arm = state.get("arm")
    if not isinstance(arm, dict) or arm.get("fire_at") is None:
        return None
    key = arm.get("key")
    if key and is_handled(state, key):
        return None
    now = time.time() if now is None else now
    re_epoch = arm.get("reset_epoch")
    if re_epoch is not None and (now - float(re_epoch)) > ARM_RESTORE_MAX_STALE:
        return None
    return {
        "kind": arm.get("kind"),
        "reset_str": arm.get("reset_str"),
        "reset_epoch": re_epoch,
        "key": key,
        "fire_at": float(arm["fire_at"]),
        "meta": arm.get("meta") or {"label": arm.get("kind")},
        "observed_at": arm.get("observed_at"),
    }


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

# Manual mode: the owner scheduled this resume time explicitly (GUI / --resume-at)
# rather than it being detected from the usage API.
MANUAL_RESUME_TEMPLATE = (
    "[AUTOMATED RESUME] A manually scheduled resume time ({reset_str}) has been "
    "reached. This is an automated message; the user is away and will NOT see or "
    "respond to your output. Continue the autonomous work from where you left "
    "off: consult _research/AUTONOMOUS_WORKLOG.md and the current todo list, and "
    "keep going. Do not wait for user input."
)


def build_message(kind: str, reset_str: str) -> str:
    # kind reaching here is session|weekly (auto) or "manual" (scheduled time).
    # monthly-spend never injects.
    if kind == "manual":
        return MANUAL_RESUME_TEMPLATE.format(reset_str=reset_str)
    return RESUME_TEMPLATE.format(kind=kind, reset_str=reset_str)


# --------------------------------------------------------------------------- #
# Transcript tailing                                                           #
# --------------------------------------------------------------------------- #


def newest_transcript(watch_dir: str):
    files = glob.glob(os.path.join(watch_dir, "*.jsonl"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def session_id_from_path(transcript_path: str):
    """The Claude Code session id is the transcript filename minus '.jsonl'
    (e.g. .../<uuid>.jsonl -> <uuid>). Returns None for a falsy path."""
    if not transcript_path:
        return None
    base = os.path.basename(transcript_path)
    return base[:-6] if base.lower().endswith(".jsonl") else base


def read_transcript_cwd(transcript_path: str):
    """Return the most-recent 'cwd' recorded in a transcript, or None.

    Each transcript line carries a "cwd" field (the session's working dir at that
    point). The LAST such value is the current cwd; its basename is the VS Code
    workspace folder (used to focus the right window for the URI injector). Reads
    only lines that contain the "cwd" token (cheap on a large jsonl)."""
    if not transcript_path:
        return None
    cwd = None
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"cwd"' not in line:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                c = obj.get("cwd")
                if isinstance(c, str) and c:
                    cwd = c
    except OSError:
        return None
    return cwd


def workspace_title_from_cwd(cwd: str):
    """The VS Code workspace folder = basename of the session's cwd (the substring
    that appears in the VS Code window title). Strips trailing separators so a cwd
    like 'X:\\repo\\' still yields 'repo'. Returns None for a falsy cwd."""
    if not cwd:
        return None
    return os.path.basename(cwd.rstrip("\\/")) or None


def derive_session_and_workspace(watch_dir: str, transcript_path=None, log=None):
    """Return (session_id, workspace_title) for the session the watcher targets.

    Prefers an explicit `transcript_path` (e.g. the one the transcript source is
    already tailing); else the newest transcript in `watch_dir` -- the session
    being actively worked in, which is the one that hit the account-wide limit.
    Either element may be None if not derivable (the caller logs + falls back)."""
    path = transcript_path or newest_transcript(watch_dir)
    if not path:
        if log:
            log(f"derive session/workspace: no transcript in {watch_dir!r}")
        return None, None
    sid = session_id_from_path(path)
    ws = workspace_title_from_cwd(read_transcript_cwd(path))
    if log:
        log(f"derive session/workspace: transcript={os.path.basename(path)} "
            f"session={sid} workspace={ws!r}")
    return sid, ws


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
# Detection sources                                                            #
#                                                                              #
# A source turns "is the account currently limited, and when does it reset?"   #
# into a uniform stream of HIT dicts the watch loop schedules. Two sources:    #
#   UsageApiSource  (primary)  -- polls the authoritative usage endpoint.      #
#   TranscriptSource (fallback) -- tails the session JSONL for a limit-hit.    #
#                                                                              #
# HIT dict shape (both sources):                                               #
#   {"kind": <label>, "reset_str": <human str | None>,                         #
#    "reset_epoch": <float | None>, "meta": <dict>, "text": <raw | optional>}  #
# reset_epoch=None AND reset_str=None -> a cap with no timed reset (spend /     #
# monthly): the loop logs "cannot auto-resume" and skips it.                   #
#                                                                              #
# Each source also answers:                                                    #
#   confirm_reset(pending, log) -> (bool, detail): at fire time, has the reset  #
#       actually been applied? (usage-API re-polls; transcript can't, so True.) #
#   loop_tick(pending, now, args) -> float: seconds to sleep before next tick.  #
# --------------------------------------------------------------------------- #


class TranscriptSource:
    """FALLBACK source: tail the session transcript JSONL for a limit-hit line.

    This is the legacy detector. Its text matching is only reachable in
    --source transcript, so the transcript self-contamination false-arm class is
    impossible in the default (usage-api) path."""

    def __init__(self, args, log):
        self.args = args
        self.log = log
        self.tailer = Tailer(args.watch_dir, from_start=args.from_start, log=log)

    def label(self):
        return "transcript"

    def poll(self, now=None):
        # Tailer already returns parsed {kind, reset_str, text, timestamp} dicts;
        # reset_epoch is resolved from reset_str by the watch loop.
        return list(self.tailer.poll())

    def confirm_reset(self, pending, log):
        # The transcript has no live usage counter to re-poll; the parsed reset
        # time has already passed, so fire.
        return True, "transcript (no live re-poll; reset time elapsed)"

    def loop_tick(self, pending, now, args):
        return args.poll


def compute_armed_interval(remaining, confirm_fast, drift_recheck, rl_wait=0.0):
    """Adaptive confirm-re-poll cadence while ARMED, given the seconds `remaining`
    until fire_at. A live 429 backoff (`rl_wait`>0) overrides everything. Far from
    the reset the account is definitely still blocked, so we barely poll (a slow
    `drift_recheck` in case resets_at moved or cleared early); as fire_at nears the
    interval shrinks so the next confirm lands ~at the reset; at/after the reset we
    poll fast (`confirm_fast`) to catch the exact clear. This is the single source
    of truth for the cadence (used by UsageApiSource and the run_watch fallback)."""
    if rl_wait and rl_wait > 0:
        return float(rl_wait)
    if remaining > 0:
        return max(float(confirm_fast), min(float(remaining), float(drift_recheck)))
    return float(confirm_fast)


class UsageApiSource:
    """PRIMARY source: poll GET /api/oauth/usage and arm on percent >= 100.

    Polls the endpoint on an ADAPTIVE cadence: IDLE at self.normal (default ~180s)
    while hunting a hit, and while ARMED it sleeps to near the known reset then
    polls fast (see armed_confirm_interval / compute_armed_interval) so the
    shared-token endpoint isn't hammered into a 429. The Usage Monitor's presence
    is only logged for information; it never changes our cadence."""

    def __init__(self, args, log, client=None, monitor_check=None):
        self.args = args
        self.log = log
        self.client = client if client is not None else usage_api.UsageClient(
            cred_path=getattr(args, "cred_path", None)
        )
        self._monitor_check = monitor_check or usage_monitor_running
        # IDLE hunt cadence (how often we poll while looking for a hit).
        self.normal = (getattr(args, "poll_interval", None)
                       or getattr(args, "usage_poll", None)
                       or IDLE_POLL_INTERVAL)
        # FAST-confirm cadence used near/after the reset while armed -- never
        # slower than the idle cadence, capped at USAGE_POLL_INTERVAL so we catch
        # the exact clear even when idle polling is deliberately slow.
        self.confirm_fast = max(1.0, min(float(self.normal), float(USAGE_POLL_INTERVAL)))
        self.drift_recheck = float(getattr(args, "armed_drift_recheck",
                                           ARMED_DRIFT_RECHECK))
        # Retained for API/test compatibility; no longer used to back off cadence.
        self.backoff = self.normal
        self.confirm_below = getattr(args, "confirm_below", CONFIRM_BELOW)
        self.monitor_proc = getattr(args, "usage_monitor_proc", USAGE_MONITOR_PROC)
        self.on_auth_expired = getattr(args, "on_auth_expired", "log")
        self._last_fetch = 0.0
        self._interval = 0.0                 # 0 -> first poll fetches immediately
        self._monitor_cache = (0.0, False)   # (checked_at, running)
        self._monitor_logged = None          # last-logged monitor state (dedup)
        self._auth_update_done = False
        self._rl_streak = 0                  # consecutive HTTP 429s (backoff ramp)
        self._rl_until = 0.0                 # epoch: no network before this (429)

    def label(self):
        return "usage-api"

    def _log_monitor_state(self, now):
        """Informational only: note when the Usage Monitor app comes/goes. Does
        NOT change our poll cadence (we always poll directly at self.normal)."""
        ts, val = self._monitor_cache
        if now - ts >= MONITOR_CACHE_TTL:
            val = bool(self._monitor_check(self.monitor_proc))
            self._monitor_cache = (now, val)
            if val != self._monitor_logged:
                state = "running" if val else "not running"
                self.log(f"MONITOR {self.monitor_proc} {state} (informational; "
                         f"autoresume polls directly every {self.normal}s "
                         f"regardless)")
                self._monitor_logged = val
        return val

    def _to_hit(self, quota):
        ra = quota.get("resets_at")
        reset_epoch = ra.timestamp() if ra else None
        reset_str = None
        if ra:
            # Human, machine-local reset string for the resume message + log.
            reset_str = datetime.fromtimestamp(reset_epoch).strftime(
                "%b %d %H:%M %Z").strip() or quota.get("resets_at_str")
        return {
            "kind": quota["label"],
            "reset_str": reset_str,
            "reset_epoch": reset_epoch,
            "meta": quota,
            "text": (f"usage-api: {quota['label']} {quota.get('percent')}% "
                     f"(severity={quota.get('severity')}, "
                     f"resets_at={quota.get('resets_at_str')})"),
        }

    def _fetch(self):
        """Fetch usage, mapping typed errors to a backoff + returning None."""
        try:
            usage = self.client.fetch()
        except usage_api.NoCredentials:
            self._interval = self.normal
            self.log("usage-api: credentials not readable; will retry")
            return None
        except usage_api.AuthExpired:
            self._interval = self.normal
            self.log("usage-api: HTTP 401 auth expired (token needs refresh); "
                     "non-fatal, will retry")
            if self.on_auth_expired == "update" and not self._auth_update_done:
                self._auth_update_done = True
                try:
                    subprocess.Popen(["claude", "update"])
                    self.log("usage-api: launched 'claude update' to refresh auth")
                except Exception as e:  # noqa: BLE001
                    self.log(f"usage-api: could not launch 'claude update' ({e!r})")
            return None
        except usage_api.RateLimited as e:
            backoff = self._note_rate_limit(e.retry_after)
            self._interval = backoff
            self.log(f"usage-api: HTTP 429 rate_limited; backing off "
                     f"{int(backoff)}s (Retry-After={e.retry_after}, "
                     f"streak={self._rl_streak})")
            return None
        except usage_api.ServerError as e:
            self._interval = min(float(self.normal), 60.0)
            self.log(f"usage-api: {e}; retrying in {int(self._interval)}s")
            return None
        except usage_api.ConnectionFailed as e:
            self._interval = min(float(self.normal), 60.0)
            self.log(f"usage-api: connection error ({e}); "
                     f"retrying in {int(self._interval)}s")
            return None
        except usage_api.UsageAPIError as e:
            self._interval = min(float(self.normal), 60.0)
            self.log(f"usage-api: {e}; retrying in {int(self._interval)}s")
            return None
        return usage

    def _note_rate_limit(self, retry_after):
        """Record an HTTP 429 and return the seconds to back off. Honors a
        Retry-After header in full; otherwise ramps 60,120,240,... capped at
        RL_BACKOFF_CAP. The backoff window (self._rl_until) is respected by BOTH
        the idle poll and the armed confirm re-poll so neither hammers the
        rate-limited endpoint."""
        self._rl_streak += 1
        if retry_after:
            backoff = max(float(retry_after), 60.0)
        else:
            backoff = min(60.0 * (2 ** (self._rl_streak - 1)), float(RL_BACKOFF_CAP))
        self._rl_until = time.time() + backoff
        return backoff

    def poll(self, now=None):
        now = time.time() if now is None else now
        if now < self._rl_until:
            return []                       # still backing off from an HTTP 429
        if self._last_fetch and (now - self._last_fetch) < self._interval:
            return []                       # respect our own polling cadence
        self._last_fetch = now
        usage = self._fetch()
        if usage is None:
            return []                       # error path already set the backoff
        # Success: clear any 429 backoff and poll again after the idle cadence.
        # The Usage Monitor's presence is only logged, never used to slow us down.
        self._rl_streak = 0
        self._rl_until = 0.0
        self._log_monitor_state(now)
        self._interval = float(self.normal)
        blocked = usage_api.find_blocked_quotas(usage)
        return [self._to_hit(q) for q in blocked]

    def armed_confirm_interval(self, remaining):
        """Adaptive network cadence while ARMED (fed the seconds until fire_at).
        A live 429 backoff overrides; otherwise see compute_armed_interval."""
        rl_wait = max(0.0, self._rl_until - time.time())
        return compute_armed_interval(remaining, self.confirm_fast,
                                      self.drift_recheck, rl_wait)

    def confirm_reset(self, pending, log):
        """RE-POLL at fire time: is the pending quota actually reset yet?"""
        meta = pending.get("meta") or {"label": pending.get("kind")}
        try:
            usage = self.client.fetch()
        except usage_api.RateLimited as e:
            backoff = self._note_rate_limit(e.retry_after)
            return False, (f"HTTP 429 at confirm (Retry-After={e.retry_after}); "
                           f"backing off {int(backoff)}s")
        except usage_api.UsageAPIError as e:
            return False, f"confirm fetch failed ({e}); waiting"
        # A successful confirm fetch clears any 429 backoff too.
        self._rl_streak = 0
        self._rl_until = 0.0
        if usage_api.is_quota_reset(usage, meta, below=self.confirm_below):
            return True, f"utilization dropped below {self.confirm_below:g}%"
        return False, (f"utilization still >= {self.confirm_below:g}%; "
                       f"reset not applied yet")

    def loop_tick(self, pending, now, args):
        # Stay responsive near a scheduled reset (so confirm retries + GUI feel
        # prompt); otherwise a coarse tick is fine (network is rate-limited above).
        if pending is not None:
            remaining = float(pending["fire_at"]) - now
            if remaining <= 300:
                return min(args.poll, CONFIRM_INTERVAL)
        return args.poll


def select_source(args, log, creds_available=None):
    """Pick the detection source per --source.

      auto        -> usage-api if credentials are readable, else transcript.
      usage-api   -> usage-api; if no credentials, log + fall back to transcript
                     (never leave an autonomous watcher dead).
      transcript  -> the legacy transcript watcher (explicit opt-in).

    `creds_available` is injectable for tests; defaults to the real check."""
    check = creds_available or (
        lambda: usage_api.credentials_available(getattr(args, "cred_path", None))
    )
    src = getattr(args, "source", "auto")

    if src == "transcript":
        log("SOURCE transcript (explicit) -- legacy fallback detector")
        return TranscriptSource(args, log)

    if src == "usage-api":
        if check():
            log("SOURCE usage-api (explicit) -- polling /api/oauth/usage")
            return UsageApiSource(args, log)
        log("SOURCE usage-api requested but no readable credentials; "
            "falling back to transcript")
        return TranscriptSource(args, log)

    # auto
    if check():
        log("SOURCE auto -> usage-api (credentials readable); polling "
            "/api/oauth/usage")
        return UsageApiSource(args, log)
    log("SOURCE auto -> transcript (no readable credentials); using the legacy "
        "transcript watcher")
    return TranscriptSource(args, log)


# --------------------------------------------------------------------------- #
# Main watch loop                                                              #
# --------------------------------------------------------------------------- #


def run_watch(args, shared=None, source=None):
    """Detect a usage-limit block and auto-resume on reset (ARM-ON-HIT machine).

    Detection is delegated to a SOURCE (usage-api primary / transcript fallback,
    chosen per --source; injectable for tests). The loop:

      * polls the source CONTINUOUSLY on a fast cadence -- for hits while idle,
        and (via confirm_reset) for the armed window CLEARING while armed;
      * ARMS the moment a hit is OBSERVED (percent >= 100) and PERSISTS the arm
        to the state file, so a restart resumes it and a reset that lands while
        the watcher is briefly down still fires;
      * FIRES the resume when the armed window CLEARS (utilization dropped back
        below the block line) OR the reset time is reached -- fires ONLY if a hit
        was observed, so a window at 0% with no prior hit never fires;
      * also honours a MANUAL resume time (GUI / --resume-at) that fires at a set
        wall-clock time regardless of API detection.

    `shared` is an optional WatchStatus the GUI reads: when provided, the loop
    PUBLISHES its state transitions (WATCHING/PENDING/FIRING/DONE + the fire_at
    the GUI counts down to) and honours GUI requests (cancel / inject-now)
    without forking any detection/inject logic."""
    def publish(**kw):
        if shared is not None:
            shared.update(**kw)

    def log(m):
        log_event(m, args.log_file)
        if shared is not None:
            shared.set_last_log(m)

    src = source if source is not None else select_source(args, log)
    manual_file = getattr(args, "manual_file", MANUAL_FILE)
    # While ARMED, the fire-on-clear confirm re-poll cadence is ADAPTIVE: the
    # source sleeps to near the known reset then polls fast (see
    # UsageApiSource.armed_confirm_interval). armed_poll is only the fixed
    # fallback for sources that don't implement the adaptive method (transcript).
    armed_poll = max(1, int(getattr(args, "usage_poll", None) or USAGE_POLL_INTERVAL))
    armed_interval = getattr(src, "armed_confirm_interval", None)
    # Fallback adaptive-cadence knobs for sources without armed_confirm_interval
    # (e.g. the transcript source / test doubles): fast near the reset, a slow
    # drift re-check far from it -- same shape as the usage-API source.
    _confirm_fast_fb = max(1.0, min(float(armed_poll), float(USAGE_POLL_INTERVAL)))
    _drift_fb = float(getattr(args, "armed_drift_recheck", ARMED_DRIFT_RECHECK))
    log(f"START autoresume v{__version__} source={src.label()} "
        f"buffer={args.buffer}s idle_poll={getattr(src, 'normal', armed_poll)}s "
        f"injector={args.injector} dry_run={args.dry_run}")
    stop_path = args.stop_file
    if stop_path and os.path.exists(stop_path):
        log(f"NOTE kill switch present at {stop_path}; injection disabled until removed")
    publish(state="WATCHING", stopped=bool(stop_path and os.path.exists(stop_path)))

    state = load_state(args.state_file)
    last_inject_time = 0.0

    # Restore a persisted arm (survives a restart; a reset missed during downtime
    # fires on the next poll). Pending: dict(kind, reset_str, reset_epoch, key,
    # fire_at, meta, observed_at).
    pending = restore_arm(state)
    if pending is not None:
        log(f"RESUME persisted arm {pending['key']} "
            f"(fire_at {datetime.fromtimestamp(pending['fire_at']).isoformat(timespec='seconds')})")
        publish(state="PENDING", kind=pending.get("kind"),
                reset_str=pending.get("reset_str"),
                reset_epoch=pending.get("reset_epoch"), fire_at=pending["fire_at"])
    else:
        clear_arm(state, args.state_file)   # drop a stale/handled persisted arm

    # CLI --resume-at seeds the shared manual-request file once (if in the future).
    ra_spec = getattr(args, "resume_at", None)
    if ra_spec:
        try:
            ra_epoch = parse_resume_at(ra_spec)
            if ra_epoch > time.time():
                write_manual_request(manual_file, ra_epoch,
                                     bool(getattr(args, "manual_only", False)))
                log(f"MANUAL --resume-at {ra_spec!r} -> fire at "
                    f"{_fmt_local(ra_epoch)} "
                    f"(manual_only={bool(getattr(args, 'manual_only', False))})")
            else:
                log(f"MANUAL --resume-at {ra_spec!r} resolves to a PAST time "
                    f"({_fmt_local(ra_epoch)}); ignoring")
        except ValueError as e:
            log(f"MANUAL --resume-at {ra_spec!r} unparseable: {e}")

    kill_logged = False          # log kill-switch HOLD only on change (no spam)
    manual_hold_logged = False   # ditto for the manual HOLD
    confirm_wait_logged = False  # log confirm-wait only on change (no spam)
    last_armed_confirm = 0.0     # last time we network-confirmed while armed

    def _session_workspace():
        """Resolve (session_id, workspace_title) for the URI injector. Explicit
        CLI overrides (--session-id / --workspace-title) win; else derive from the
        transcript the source is tailing (transcript source) or the newest
        transcript in the watch dir (usage-api source has no tailer)."""
        sid = getattr(args, "session_id", None)
        ws = getattr(args, "workspace_title", None)
        if sid and ws:
            return sid, ws
        tailer = getattr(src, "tailer", None)
        tpath = getattr(tailer, "path", None) if tailer is not None else None
        d_sid, d_ws = derive_session_and_workspace(args.watch_dir, tpath, log=log)
        return (sid or d_sid), (ws or d_ws)

    def do_inject(kind, reset_str):
        nonlocal last_inject_time
        message = build_message(kind, reset_str)
        injector = args.injector
        session_id = workspace_title = None
        if injector == "uri":
            session_id, workspace_title = _session_workspace()
            if not session_id:
                log("inject(uri): no session id derivable; falling back to "
                    "--injector win32 (keystroke) for this fire")
                injector = "win32"
        if args.dry_run:
            if injector == "uri":
                url = build_resume_uri(session_id, message)
                log(f"DRY-RUN would inject via URI (session={session_id} "
                    f"workspace={workspace_title!r}): {url}")
            else:
                log(f"DRY-RUN would inject ({kind}) via {injector}: {message}")
            last_inject_time = time.time()
            return True
        if injector == "ahk":
            ok, detail = inject_via_ahk(message, args.ahk_exe, args.ahk_script, log)
        elif injector == "uri":
            ok, detail = inject_via_uri(
                message, session_id, workspace_title,
                proc_name=args.target_proc, title_substr=args.target_title,
                prefer_substr=(args.prefer_title or workspace_title),
                extra_enter=bool(getattr(args, "uri_extra_enter", False)),
                log=log)
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
        # never fires and drop it + its persisted arm). No-op when idle.
        if shared is not None and shared.take_cancel():
            if pending is not None:
                log(f"CANCEL pending {pending['key']} by GUI request")
                mark_handled(state, pending["key"])
                clear_arm(state, args.state_file)      # also saves state
                pending = None
                confirm_wait_logged = False
                publish(state="WATCHING", fire_at=None, kind=None, reset_str=None,
                        reset_epoch=None, last_action="cancelled pending reset")

        # ---- Manual resume request (GUI / CLI shared file) ------------------- #
        manual = read_manual_request(manual_file)
        manual_only = bool(getattr(args, "manual_only", False)) or bool(
            manual and manual.get("manual_only"))
        publish(manual_resume_at=(manual["resume_at"] if manual else None),
                manual_only=manual_only)

        now = time.time()

        # Show a COUNTDOWN to a pending manual resume time so a set manual time is
        # visibly armed in the GUI (a timer), not just a status line. An auto arm,
        # if present, owns the countdown (its fire_at is the real reset+buffer); a
        # manual time only drives the display when nothing is auto-armed.
        if manual is not None and pending is None:
            m_at = float(manual["resume_at"])
            if now < m_at:
                publish(state="PENDING", kind="manual",
                        reset_str=_fmt_local(m_at), reset_epoch=m_at, fire_at=m_at)

        # ---- MANUAL FIRE: fire at the owner-set time regardless of detection -- #
        if manual is not None:
            m_fire = float(manual["resume_at"])
            if now >= m_fire:
                mkey = f"manual|{int(round(m_fire / 60.0)) * 60}"
                if is_handled(state, mkey):
                    clear_manual_request(manual_file)   # already fired; drop it
                elif stopped:
                    if not manual_hold_logged:
                        log(f"HOLD manual resume {mkey}: kill switch present at "
                            f"{args.stop_file}; will inject once removed")
                        manual_hold_logged = True
                elif (now - last_inject_time) >= args.min_interval:
                    manual_hold_logged = False
                    log(f"FIRE manual resume {mkey} (scheduled {_fmt_local(m_fire)})")
                    publish(state="FIRING")
                    ok = do_inject("manual", _fmt_local(m_fire))
                    if ok:
                        mark_handled(state, mkey)
                        clear_manual_request(manual_file)
                        # one resume suffices: drop any auto arm too.
                        pending = None
                        clear_arm(state, args.state_file)   # also saves state
                        confirm_wait_logged = False
                        log(f"DONE injected+recorded {mkey}")
                        publish(state="DONE", fire_at=None,
                                last_action=f"injected manual ({mkey})")
                    else:
                        log(f"RETRY-LATER manual inject failed for {mkey}; "
                            f"will retry next poll")
                        publish(state="PENDING",
                                last_action=f"manual inject failed for {mkey}; retrying")

        # ---- AUTO ARM: discover a block only while idle (and not manual-only) - #
        # Once armed we stop hunting for new hits and instead watch the armed
        # window for its CLEAR (below); the account stays limited until it resets.
        if not manual_only and pending is None:
            for hit in src.poll():
                kind = hit.get("kind")
                reset_str = hit.get("reset_str")
                raw = hit.get("text", reset_str)
                reset_epoch = hit.get("reset_epoch")
                if reset_epoch is None and reset_str:
                    try:
                        reset_epoch, _dt = resolve_reset_epoch(reset_str)
                    except Exception as e:  # noqa: BLE001
                        log(f"WARN could not resolve reset {reset_str!r}: {e}")
                        continue
                if reset_epoch is None:
                    # A cap with no timed reset (spend / monthly). Cannot
                    # auto-resume; log once per day (retry storms repeat it).
                    mkey = f"{kind}|" + datetime.now().strftime("%Y-%m-%d")
                    if not is_handled(state, mkey):
                        log(f"DETECT {kind} (no timed reset / billing cap) -- "
                            f"CANNOT auto-resume, skipping. raw={raw!r}")
                        mark_handled(state, mkey)
                        save_state(state, args.state_file)
                    continue
                key = handled_key(kind, reset_epoch)
                local_str = datetime.fromtimestamp(reset_epoch).isoformat(timespec="seconds")
                log(f"DETECT {kind} limit; reset_reported={reset_str!r} "
                    f"resolved_local={local_str} key={key}")
                now = time.time()
                if is_handled(state, key):
                    # Normally once-per-reset dedup. BUT this hit means the quota
                    # is CURRENTLY blocked (>=100%). If its reset is still well in
                    # the FUTURE, a prior fire marked it handled before the reset
                    # actually happened (e.g. inject-now while still limited, or a
                    # premature confirm) -> the account is STILL limited and WILL
                    # reset at this epoch. Clear the stale mark and re-arm so the
                    # real reset still resumes; don't leave it dead. A legitimate
                    # post-reset fire drops utilization <100 so the quota isn't
                    # reported here at all -> no double-fire after a real reset.
                    if reset_epoch > now + FUTURE_REARM_GRACE:
                        log(f"RE-ARM {key}: was marked handled but the quota is "
                            f"still blocked and its reset is "
                            f"{int(reset_epoch - now)}s in the FUTURE "
                            f"(prior fire before the actual reset); clearing the "
                            f"stale handled mark and re-arming")
                        state["handled"] = [h for h in state.get("handled", [])
                                            if h != key]
                        save_state(state, args.state_file)
                        # fall through to the stale check + arm below
                    else:
                        log(f"SKIP already handled {key} (dedup, once per reset)")
                        continue
                if reset_epoch < now - args.stale_grace:
                    log(f"SKIP stale reset {key} (already passed > "
                        f"{args.stale_grace}s ago; historical entry)")
                    mark_handled(state, key)
                    save_state(state, args.state_file)
                    continue
                fire_at = reset_epoch + args.buffer
                # Prefer the latest (largest fire_at) if multiple quotas block:
                # the account stays limited until the LAST of them resets.
                if pending is None or fire_at > pending["fire_at"]:
                    pending = {
                        "kind": kind, "reset_str": reset_str,
                        "reset_epoch": reset_epoch, "key": key, "fire_at": fire_at,
                        "meta": hit.get("meta"), "observed_at": now,
                    }
                    confirm_wait_logged = False
                    last_armed_confirm = 0.0
                    save_arm(state, pending, args.state_file)   # PERSIST the arm
                    log(f"ARM {key} at "
                        f"{datetime.fromtimestamp(fire_at).isoformat(timespec='seconds')} "
                        f"(reset+{args.buffer}s); armed on observed hit, persisted")
                    publish(state="PENDING", kind=kind, reset_str=reset_str,
                            reset_epoch=reset_epoch, fire_at=fire_at)

        # GUI request: fire the current pending injection immediately (bypass the
        # remaining countdown AND the reset-confirm re-poll -- the owner asked for
        # it explicitly). Still routed through the SAME guarded do_inject below.
        force_now = bool(shared is not None and shared.take_inject_now())

        # GUI "Inject now" with NO armed window (plain watching mode): the button
        # used to do nothing here, because the entire fire block below is gated on
        # `pending is not None`. Fire an immediate manual inject in that case.
        if force_now and pending is None:
            if stopped:
                if not kill_logged:
                    log(f"HOLD inject-now: kill switch present at {args.stop_file}; "
                        f"remove it and click again to inject")
                    kill_logged = True
                publish(last_action="inject-now held (kill switch)")
            else:
                kill_logged = False
                log("FIRE inject-now (manual; no pending window)")
                publish(state="FIRING")
                ok = do_inject("manual", _fmt_local(time.time()))
                log("DONE inject-now injected" if ok
                    else "inject-now FAILED (injector returned False -- check the "
                         "target window title/proc and focus_method)")
                publish(state="WATCHING",
                        last_action="manual inject-now" if ok
                        else "inject-now FAILED")
            force_now = False   # consumed; the pending branch handles pending!=None

        # ---- AUTO FIRE: fire when the armed window CLEARS or its time arrives - #
        if not manual_only and pending is not None:
            now = time.time()
            fire = False
            reason = ""
            # FIRE-ON-CLEAR: while armed we re-poll and fire the instant the
            # source reports the window cleared (utilization back below the block
            # line). This catches an early / exact reset and a reset that landed
            # while we were restarting -- not just reset+buffer. The cadence is
            # ADAPTIVE (sleep to near the known reset, then poll fast) so we don't
            # hammer the shared-token endpoint into a 429 for the whole arm.
            remaining = float(pending["fire_at"]) - now
            confirm_iv = (armed_interval(remaining) if armed_interval is not None
                          else compute_armed_interval(remaining, _confirm_fast_fb,
                                                       _drift_fb))
            confirm_due = (force_now or last_armed_confirm == 0.0
                           or (now - last_armed_confirm) >= confirm_iv)
            if force_now:
                fire, reason = True, "inject-now"
            elif confirm_due:
                last_armed_confirm = now
                confirmed, detail = src.confirm_reset(pending, log)
                if confirmed:
                    fire, reason = True, f"window cleared ({detail})"
                    confirm_wait_logged = False
                elif now >= pending["fire_at"]:
                    # Reset time reached but the server has not applied its own
                    # reset yet -> never resume into a still-blocked account; wait.
                    if not confirm_wait_logged:
                        log(f"WAIT {pending['key']}: reset not applied yet "
                            f"({detail}); re-checking every {int(confirm_iv)}s")
                        confirm_wait_logged = True
                    publish(state="PENDING",
                            last_action=f"reset not applied yet ({detail})")

            if fire:
                key = pending["key"]
                if stopped:
                    # Kill switch present: HOLD the injection until it is lifted.
                    if not kill_logged:
                        log(f"HOLD injection {key}: kill switch present at "
                            f"{args.stop_file}; will inject once removed")
                        kill_logged = True
                    # do not fire this tick; re-evaluate after the loop sleep
                elif not force_now and (now - last_inject_time) < args.min_interval:
                    pass    # global min-interval backstop; retry next tick
                elif is_handled(state, key):
                    log(f"SKIP {key} became handled before fire")
                    clear_arm(state, args.state_file)
                    pending = None
                    confirm_wait_logged = False
                    publish(state="DONE", fire_at=None)
                else:
                    kill_logged = False
                    log(f"FIRE inject {key} (kind={pending['kind']}; {reason})"
                        + (" [inject-now]" if force_now else ""))
                    publish(state="FIRING")
                    ok = do_inject(pending["kind"], pending["reset_str"])
                    if ok:
                        mark_handled(state, key)
                        clear_arm(state, args.state_file)   # also saves state
                        log(f"DONE injected+recorded {key}")
                        publish(state="DONE", fire_at=None,
                                last_action=f"injected {pending['kind']} ({key})")
                        pending = None
                        confirm_wait_logged = False
                    else:
                        log(f"RETRY-LATER inject failed for {key}; "
                            f"will retry next poll")
                        publish(state="PENDING",
                                last_action=f"inject failed for {key}; retrying")

        time.sleep(src.loop_tick(pending, time.time(), args))


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
            "manual_resume_at": None,  # owner-set manual resume time (epoch) or None
            "manual_only": False,      # manual-only (auto-detection suppressed)
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
    manual_resume_at = snap.get("manual_resume_at")
    manual_only = bool(snap.get("manual_only"))

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

    manual_line = ""
    if manual_resume_at:
        manual_line = (f"Manual resume {_fmt_local(manual_resume_at)}"
                       + ("  (manual-only)" if manual_only else "  (backstop: auto on)"))

    return {
        "headline": headline,
        "countdown": countdown,
        "remaining": remaining,
        "detail": detail,
        "status_line": status,
        "kind": kind,
        "reset_str": reset_str,
        "manual_resume_at": manual_resume_at,
        "manual_only": manual_only,
        "manual_line": manual_line,
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

    def __init__(self, root, shared: WatchStatus, stop_file: str,
                 manual_file: str = None, tick_ms: int = 1000):
        self.root = root
        self.shared = shared
        self.stop_file = stop_file
        self.manual_file = manual_file or MANUAL_FILE
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
        # Manual resume-time control.
        self.var_manual_entry = tk.StringVar(value="")
        self.var_manual_status = tk.StringVar(value="Manual resume: not set")
        self.var_manual_only = tk.IntVar(value=0)

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

        # -- Manual resume-time control -------------------------------------- #
        # A divider + a labelled row: "Resume at [ HH:MM|+Nm|ISO ] [Set][Clear]".
        # Set writes the shared manual-request file; the watch loop fires at that
        # time regardless of API detection. Clear removes it.
        tk.Frame(root, bg="#333333", height=1).pack(fill="x", pady=(2, 4), **pad)
        man = tk.Frame(root, bg=_BG)
        man.pack(fill="x", pady=(0, 2), **pad)
        tk.Label(man, text="Resume at", font=small, bg=_BG, fg=_FG).pack(side="left")
        self.ent_manual = tk.Entry(man, textvariable=self.var_manual_entry, width=14,
                                   font=small, bg="#2a2a2a", fg=_FG,
                                   insertbackground=_FG, relief="flat")
        self.ent_manual.pack(side="left", padx=(6, 6))
        self.ent_manual.bind("<Return>", lambda _e: self.set_manual())
        tk.Button(man, text="Set", width=5, command=self.set_manual).pack(side="left")
        tk.Button(man, text="Clear", width=6,
                  command=self.clear_manual).pack(side="left", padx=(4, 0))
        opt = tk.Frame(root, bg=_BG)
        opt.pack(fill="x", **pad)
        tk.Checkbutton(opt, text="manual only (suppress auto-detect)",
                       variable=self.var_manual_only, font=small, bg=_BG, fg=_SUBFG,
                       selectcolor=_BG, activebackground=_BG, activeforeground=_FG,
                       command=self._reapply_manual_only).pack(side="left")
        self.lbl_manual = tk.Label(root, textvariable=self.var_manual_status,
                                   font=small, bg=_BG, fg=_SUBFG, anchor="w",
                                   justify="left", wraplength=380)
        self.lbl_manual.pack(fill="x", pady=(2, 4), **pad)

        tk.Label(root, textvariable=self.var_lastlog, font=mono, bg=_BG, fg="#6a6a6a",
                 anchor="w", justify="left", wraplength=380).pack(fill="x",
                                                                  pady=(0, 8), **pad)

    # -- rendering --------------------------------------------------------- #
    def refresh(self):
        snap = self.shared.snapshot()
        # The stop-file is the source of truth for the kill switch; check it live
        # so Pause/Resume reflects INSTANTLY (not at the next watch poll).
        snap["stopped"] = bool(self.stop_file and os.path.exists(self.stop_file))
        # The manual-request FILE is the source of truth for the scheduled manual
        # time (so it reflects a headless set / a set from another process too).
        mreq = read_manual_request(self.manual_file)
        snap["manual_resume_at"] = mreq["resume_at"] if mreq else None
        snap["manual_only"] = bool(mreq and mreq.get("manual_only"))
        view = derive_view(snap, time.time())

        self.var_state.set(view["headline"])
        self.lbl_state.configure(fg=_STATE_COLORS.get(view["headline"], _FG))
        self.var_countdown.set(view["countdown"] or "--:--:--")
        self.var_status.set(view["status_line"])
        self.var_detail.set(view["detail"])
        last = snap.get("last_log") or ""
        self.var_lastlog.set(last if len(last) <= 90 else last[:87] + "…")
        self.var_pause.set("Resume" if snap["stopped"] else "Pause")
        self.var_manual_status.set(view["manual_line"] or "Manual resume: not set")
        self.lbl_manual.configure(fg="#f0c040" if view["manual_resume_at"] else _SUBFG)

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

    # -- manual resume-time handlers --------------------------------------- #
    def set_manual(self):
        """Parse the entry (HH:MM | +Nm | ISO) and write the manual-request file.

        The watch loop reads the file each tick and fires the resume at that time
        regardless of API detection. Invalid input shows a red error line."""
        spec = (self.var_manual_entry.get() or "").strip()
        if not spec:
            self.var_manual_status.set("Manual resume: enter HH:MM, +Nm, or ISO")
            self.lbl_manual.configure(fg="#e06666")
            return
        try:
            epoch = parse_resume_at(spec)
        except ValueError as e:
            self.var_manual_status.set(f"Manual resume: {e}")
            self.lbl_manual.configure(fg="#e06666")
            return
        try:
            write_manual_request(self.manual_file, epoch,
                                 bool(self.var_manual_only.get()))
        except OSError as e:
            self.var_manual_status.set(f"Manual resume: write failed ({e})")
            self.lbl_manual.configure(fg="#e06666")
            return
        self.var_manual_status.set(
            f"Manual resume {_fmt_local(epoch)}"
            + ("  (manual-only)" if self.var_manual_only.get()
               else "  (backstop: auto on)"))
        self.lbl_manual.configure(fg="#f0c040")

    def clear_manual(self):
        """Remove the manual-request file (cancel the scheduled manual resume)."""
        clear_manual_request(self.manual_file)
        self.var_manual_entry.set("")
        self.var_manual_status.set("Manual resume: not set")
        self.lbl_manual.configure(fg=_SUBFG)

    def _reapply_manual_only(self):
        """If a manual time is already set, re-write it so a manual-only toggle
        change takes effect immediately."""
        mreq = read_manual_request(self.manual_file)
        if mreq:
            try:
                write_manual_request(self.manual_file, mreq["resume_at"],
                                     bool(self.var_manual_only.get()))
            except OSError:
                pass


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
    win = StatusWindow(root, shared, args.stop_file,
                       manual_file=getattr(args, "manual_file", MANUAL_FILE),
                       tick_ms=1000)
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
    the injection path). With --injector uri it targets an EXACT session tab by
    id (derived, or forced via --session-id / --workspace-title); --dry-run logs
    the URI without firing it."""
    log = lambda m: log_event(m, args.log_file)
    if args.injector == "uri":
        session_id = getattr(args, "session_id", None)
        workspace_title = getattr(args, "workspace_title", None)
        if not (session_id and workspace_title):
            d_sid, d_ws = derive_session_and_workspace(args.watch_dir, log=log)
            session_id = session_id or d_sid
            workspace_title = workspace_title or d_ws
        if getattr(args, "dry_run", False):
            url = build_resume_uri(session_id, args.message)
            log(f"DRY-RUN would inject via URI (session={session_id} "
                f"workspace={workspace_title!r}): {url}")
            print(f"inject ok=True detail=dry-run session={session_id}")
            print(f"URI: {url}")
            return 0
        ok, detail = inject_via_uri(
            args.message, session_id, workspace_title,
            proc_name=args.target_proc, title_substr=args.target_title,
            prefer_substr=(args.prefer_title or workspace_title),
            press_enter=not args.no_enter,
            extra_enter=bool(getattr(args, "uri_extra_enter", False)), log=log)
    elif args.injector == "ahk":
        if getattr(args, "dry_run", False):
            print(f"inject ok=True detail=dry-run (ahk) message={args.message!r}")
            return 0
        ok, detail = inject_via_ahk(args.message, args.ahk_exe, args.ahk_script, log)
    else:
        if getattr(args, "dry_run", False):
            print(f"inject ok=True detail=dry-run (win32) message={args.message!r}")
            return 0
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
        sp.add_argument("--injector", choices=["uri", "win32", "ahk"],
                        default="uri",
                        help="injection mechanism. uri (default)=fire the "
                             "vscode://anthropic.claude-code/open?session=<id> "
                             "deep link to target the EXACT session tab, then one "
                             "Enter to submit; win32=legacy keystroke injector "
                             "(focus a window + type into the active tab); "
                             "ahk=AutoHotkey v2 fallback")
        sp.add_argument("--session-id", default=None,
                        help="uri injector: force this Claude Code session id "
                             "(default: derive from the newest/tailed transcript)")
        sp.add_argument("--workspace-title", default=None,
                        help="uri injector: force the VS Code workspace-folder "
                             "substring used to focus the right window (default: "
                             "basename of the session's cwd)")
        sp.add_argument("--uri-extra-enter", action="store_true",
                        help="uri injector: send a SECOND Enter after a settle "
                             "(for setups where a one-time 'open external app?' "
                             "confirmation eats the first Enter)")
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

    w = sub.add_parser("watch", help="detect a usage-limit block and auto-resume")
    add_common(w)
    w.add_argument("--source", choices=["auto", "usage-api", "transcript"],
                   default="auto",
                   help="detection source. auto=usage-api if credentials are "
                        "readable else transcript; usage-api=poll "
                        "/api/oauth/usage (primary); transcript=legacy JSONL "
                        "watcher (fallback)")
    w.add_argument("--poll-interval", "--usage-poll", dest="usage_poll", type=int,
                   default=IDLE_POLL_INTERVAL,
                   help="IDLE usage-endpoint poll cadence in seconds (default "
                        f"{IDLE_POLL_INTERVAL}). While ARMED the confirm re-poll "
                        "is ADAPTIVE (sleep to near the known reset, then poll "
                        f"fast, capped at {USAGE_POLL_INTERVAL}s) so the "
                        "shared-token endpoint isn't hammered into a 429. "
                        "(--usage-poll is a legacy alias.)")
    w.add_argument("--armed-drift-recheck", type=int, default=ARMED_DRIFT_RECHECK,
                   help="while ARMED but far from the reset, re-confirm at most "
                        f"this often in seconds (default {ARMED_DRIFT_RECHECK}; "
                        "safety net for a resets_at that moved or cleared early)")
    w.add_argument("--monitor-poll", type=int, default=MONITOR_POLL_INTERVAL,
                   help="DEPRECATED / ignored -- autoresume no longer backs off "
                        "for the Usage Monitor; it always polls at --poll-interval")
    w.add_argument("--confirm-interval", type=int, default=CONFIRM_INTERVAL,
                   help="re-poll cadence to confirm a reset was applied before "
                        f"injecting (default {CONFIRM_INTERVAL})")
    w.add_argument("--resume-at", default=None,
                   help="MANUAL resume time -- fire at this time regardless of "
                        "API detection. Accepts HH:MM (next occurrence), +Nm "
                        "(minutes from now; +Nh / +Ns too), or an ISO timestamp. "
                        "Auto-detection stays on as a backstop unless --manual-only.")
    w.add_argument("--manual-only", action="store_true",
                   help="fire ONLY the manual --resume-at / GUI time; suppress "
                        "usage-API auto-detection while set")
    w.add_argument("--manual-file", default=MANUAL_FILE,
                   help="shared manual-request file the GUI and CLI use to schedule "
                        f"a manual resume time (default {MANUAL_FILE})")
    w.add_argument("--confirm-below", type=float, default=CONFIRM_BELOW,
                   help="a quota's utilization must drop below this %% to confirm "
                        f"its reset (default {CONFIRM_BELOW:g})")
    w.add_argument("--usage-monitor-proc", default=USAGE_MONITOR_PROC,
                   help="process image name of the Usage Monitor app to detect")
    w.add_argument("--on-auth-expired", choices=["log", "update"], default="log",
                   help="on HTTP 401: just log (default), or launch 'claude "
                        "update' once to refresh the token")
    w.add_argument("--cred-path", default=None,
                   help="override the credentials file path (default "
                        "$CLAUDE_CONFIG_DIR/.credentials.json or "
                        "~/.claude/.credentials.json)")
    w.add_argument("--from-start", action="store_true",
                   help="transcript source only: parse from the beginning "
                        "(default: tail from EOF)")
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
    inj.add_argument("--dry-run", action="store_true",
                     help="log/print what would be injected (e.g. the exact URI) "
                          "without firing it")

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
