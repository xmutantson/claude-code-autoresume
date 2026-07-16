#!/usr/bin/env python3
"""usage_api.py -- authoritative Claude usage/limit source (credential + network).

This module is the SINGLE place in autoresume that reads the OAuth credential and
talks to the network. Everything else (scheduling, injection, GUI) consumes the
pure data it returns. Isolating credentials + I/O here mirrors the api.py
isolation in jens-duttke/usage-monitor-for-claude (MIT), whose detection approach
this adapts.

Data source (same endpoint the Usage Monitor for Claude uses):

    GET https://api.anthropic.com/api/oauth/usage

Auth: the OAuth access token from ``$CLAUDE_CONFIG_DIR/.credentials.json`` (else
``~/.claude/.credentials.json``) -> JSON ["claudeAiOauth"]["accessToken"].

SECURITY: the access token is NEVER logged, printed, stored, or placed in any
exception message. It exists only in-process, only in the Authorization header of
a single request, and is re-read on every fetch so a token the CLI refreshes is
picked up automatically.

Response schema (verified live):
    { "five_hour":  {"utilization": 91.0, "resets_at": "<iso8601>", "severity": ...},
      "seven_day":  {"utilization": 38.0, "resets_at": "<iso8601>", ...},
      "seven_day_opus"|"seven_day_sonnet"|"seven_day_cowork": null | {...},
      "limits": [ {"kind":"session","group":"session","percent":91,
                   "severity":"critical","resets_at":"<iso8601>","is_active":true},
                  {"kind":"weekly_all","group":"weekly","percent":38, ...}, ... ],
      "extra_usage": {...}, "spend": {...}, "member_dashboard_available": false }

A quota is BLOCKED when its percent/utilization is >= 100 (severity may also
escalate normal -> warning -> critical -> exhausted, but >=100 is the definitive
block; 91%/critical does NOT arm). Spend / monthly caps have no timed reset and
cannot be auto-resumed -> they surface with resets_at=None so the caller logs +
skips them.

Standard library only (urllib) -- no third-party packages.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
ANTHROPIC_BETA = "oauth-2025-04-20"
DEFAULT_CLI_VERSION = "2.1.85"          # User-Agent fallback if `claude --version` fails

# Windows: suppress the console-window flash a subprocess of a console program
# (claude --version) makes even under pythonw. 0 (no-op) on non-Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

BLOCK_THRESHOLD = 100.0                 # percent/utilization >= this -> BLOCKED
RESET_CONFIRM_BELOW = 90.0              # at fire time, quota must drop below this
                                        # to confirm the server applied the reset

_EXHAUSTED_SEVERITIES = {"exhausted"}   # severities that mean blocked regardless
                                        # of the numeric percent (belt-and-braces)


# --------------------------------------------------------------------------- #
# Errors                                                                       #
# --------------------------------------------------------------------------- #

class UsageAPIError(Exception):
    """Base class for all usage-API failures. Messages never contain the token."""


class NoCredentials(UsageAPIError):
    """No readable OAuth access token (missing/invalid credentials file)."""


class AuthExpired(UsageAPIError):
    """HTTP 401 -- the OAuth token is expired/invalid and needs refreshing."""


class RateLimited(UsageAPIError):
    """HTTP 429 -- respect Retry-After (seconds) and back off."""

    def __init__(self, retry_after=None):
        self.retry_after = retry_after
        super().__init__(f"rate_limited (retry_after={retry_after})")


class ServerError(UsageAPIError):
    """HTTP 5xx -- transient server error, retry."""


class ConnectionFailed(UsageAPIError):
    """Network/timeout error reaching the endpoint, retry."""


# --------------------------------------------------------------------------- #
# Credentials (token isolation)                                                #
# --------------------------------------------------------------------------- #

def credentials_path(cred_path: str | None = None) -> str:
    """Path to the credentials file: explicit override, else
    ``$CLAUDE_CONFIG_DIR/.credentials.json``, else ``~/.claude/.credentials.json``."""
    if cred_path:
        return cred_path
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude"
    )
    return os.path.join(base, ".credentials.json")


def read_access_token(cred_path: str | None = None):
    """Return the OAuth access token string, or None if unreadable.

    NEVER logs or prints the token. Callers must keep it out of logs too."""
    p = credentials_path(cred_path)
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    tok = (data.get("claudeAiOauth") or {}).get("accessToken")
    return tok or None


def credentials_available(cred_path: str | None = None) -> bool:
    """True iff an OAuth access token can be read (source-selection gate)."""
    return read_access_token(cred_path) is not None


# --------------------------------------------------------------------------- #
# CLI version detection (for the User-Agent)                                   #
# --------------------------------------------------------------------------- #

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


def detect_cli_version(fallback: str = DEFAULT_CLI_VERSION) -> str:
    """Return the installed Claude Code CLI version (e.g. '2.1.76'), for the
    ``claude-code/<version>`` User-Agent. Falls back to `fallback` on any error."""
    try:
        out = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=10,
            creationflags=_NO_WINDOW,
        )
        m = _VERSION_RE.search((out.stdout or "") + (out.stderr or ""))
        if m:
            return m.group(1)
    except Exception:  # noqa: BLE001 - version detection is best-effort
        pass
    return fallback


def build_headers(token: str, cli_version: str) -> dict:
    """Build the request headers. The token lives only here and in the request."""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": f"claude-code/{cli_version}",
        "anthropic-beta": ANTHROPIC_BETA,
    }


# --------------------------------------------------------------------------- #
# Network: the ONE request                                                     #
# --------------------------------------------------------------------------- #

def _parse_retry_after(value):
    """Parse a Retry-After header (delta-seconds) into a float, else None."""
    if not value:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None  # HTTP-date form: fall back to caller's default backoff


def fetch_usage(token=None, cli_version=None, timeout=20, url=USAGE_URL,
                cred_path=None) -> dict:
    """GET the usage endpoint and return the parsed JSON dict.

    Reads the token itself (never accepts it in a log). Raises the typed errors
    above on 401/429/5xx/connection/no-credentials. The token is not included in
    any exception message."""
    tok = token or read_access_token(cred_path)
    if not tok:
        raise NoCredentials("no OAuth access token available")
    ver = cli_version or DEFAULT_CLI_VERSION
    req = urllib.request.Request(url, headers=build_headers(tok, ver), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        return json.loads(body)
    except urllib.error.HTTPError as e:
        code = getattr(e, "code", 0)
        if code == 401:
            raise AuthExpired("HTTP 401 (token expired/invalid)") from None
        if code == 429:
            ra = None
            try:
                ra = _parse_retry_after(e.headers.get("Retry-After"))
            except Exception:  # noqa: BLE001
                ra = None
            raise RateLimited(ra) from None
        if 500 <= code < 600:
            raise ServerError(f"HTTP {code}") from None
        raise UsageAPIError(f"HTTP {code}") from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
        # Deliberately do NOT chain (from e) or embed the request -- keep the
        # token out of any traceback. Only the reason string is surfaced.
        reason = getattr(e, "reason", None) or e.__class__.__name__
        raise ConnectionFailed(str(reason)) from None
    except ValueError as e:  # json decode
        raise UsageAPIError(f"bad JSON response ({e})") from None


class UsageClient:
    """Thin stateful wrapper: caches the CLI version, re-reads the token each
    fetch. Injected into the watcher so tests can substitute a fake."""

    def __init__(self, cred_path=None, cli_version=None, url=USAGE_URL, timeout=20):
        self._cred_path = cred_path
        self._version = cli_version           # lazily detected on first use
        self._url = url
        self._timeout = timeout

    @property
    def version(self) -> str:
        if self._version is None:
            self._version = detect_cli_version()
        return self._version

    def fetch(self) -> dict:
        return fetch_usage(
            cli_version=self.version, timeout=self._timeout,
            url=self._url, cred_path=self._cred_path,
        )


# --------------------------------------------------------------------------- #
# Pure interpretation (no network / no credentials) -- unit-testable           #
# --------------------------------------------------------------------------- #

_KIND_LABELS = {
    "session": "session", "five_hour": "session",
    "weekly_all": "weekly", "seven_day": "weekly",
    "weekly_opus": "weekly (Opus)", "seven_day_opus": "weekly (Opus)",
    "weekly_sonnet": "weekly (Sonnet)", "seven_day_sonnet": "weekly (Sonnet)",
    "weekly_cowork": "weekly (cowork)", "seven_day_cowork": "weekly (cowork)",
    "weekly_scoped": "weekly (scoped)",
}

_TOP_LEVEL_KEYS = (
    "five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet",
    "seven_day_cowork",
)


def friendly_label(kind, group) -> str:
    """Map a raw (kind, group) to a human label used in logs + the resume message."""
    if kind in _KIND_LABELS:
        return _KIND_LABELS[kind]
    if group == "session":
        return "session"
    if group == "weekly":
        return f"weekly ({kind})" if kind else "weekly"
    if group in ("monthly", "spend") or (
        kind and ("spend" in kind or "monthly" in kind)
    ):
        return "spend cap"
    return kind or group or "usage"


def parse_resets_at(value):
    """Parse an ISO8601 reset timestamp into a tz-aware UTC datetime, or None.

    Handles the microsecond + offset form the API returns
    ('2026-07-02T07:09:59.117970+00:00') and a trailing 'Z'. A naive timestamp is
    assumed UTC. Returns None for missing/unparseable input."""
    if not value:
        return None
    t = str(value).strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _make_quota(label, kind, group, percent, resets_at, resets_at_raw, severity,
                source):
    return {
        "label": label,
        "kind": kind,
        "group": group,
        "percent": percent,
        "resets_at": resets_at,                 # tz-aware datetime or None
        "resets_at_str": resets_at_raw,         # original ISO string or None
        "severity": severity,
        "source": source,                       # "limits" or "top-level"
    }


def _dedup_key(quota):
    ra = quota.get("resets_at")
    minute = int(ra.timestamp() // 60) if ra else None
    return (quota["label"], minute)


def _is_blocked(percent, severity, threshold):
    if percent is not None and float(percent) >= threshold:
        return True
    if severity and str(severity).lower() in _EXHAUSTED_SEVERITIES:
        return True
    return False


def iter_quota_utilizations(usage: dict):
    """Yield {label, percent, resets_at} for every quota reported (limits[] +
    the known top-level objects), regardless of threshold. Used by
    is_quota_reset to look up a quota's CURRENT utilization."""
    for lim in usage.get("limits") or []:
        pct = lim.get("percent")
        if pct is None:
            continue
        yield {
            "label": friendly_label(lim.get("kind"), lim.get("group")),
            "percent": float(pct),
            "resets_at": parse_resets_at(lim.get("resets_at")),
        }
    for key in _TOP_LEVEL_KEYS:
        obj = usage.get(key)
        if not isinstance(obj, dict):
            continue
        util = obj.get("utilization")
        if util is None:
            continue
        yield {
            "label": friendly_label(key, key),
            "percent": float(util),
            "resets_at": parse_resets_at(obj.get("resets_at")),
        }


def find_blocked_quotas(usage: dict, threshold: float = BLOCK_THRESHOLD):
    """Return the list of BLOCKED quota dicts (percent/utilization >= threshold,
    or an exhausted severity). Both the limits[] entries and the top-level
    five_hour/seven_day* objects are scanned; duplicates for the same quota
    (same label + reset minute) collapse. Non-blocked quotas (e.g. 91%/critical,
    38%/normal) are omitted -> caller does NOT arm."""
    out = []
    seen = set()

    for lim in usage.get("limits") or []:
        pct = lim.get("percent")
        if not _is_blocked(pct, lim.get("severity"), threshold):
            continue
        kind = lim.get("kind")
        group = lim.get("group")
        ra_raw = lim.get("resets_at")
        q = _make_quota(
            friendly_label(kind, group), kind, group,
            float(pct) if pct is not None else None,
            parse_resets_at(ra_raw), ra_raw, lim.get("severity"), "limits",
        )
        k = _dedup_key(q)
        if k in seen:
            continue
        seen.add(k)
        out.append(q)

    for key in _TOP_LEVEL_KEYS:
        obj = usage.get(key)
        if not isinstance(obj, dict):
            continue
        util = obj.get("utilization")
        if not _is_blocked(util, obj.get("severity"), threshold):
            continue
        ra_raw = obj.get("resets_at")
        q = _make_quota(
            friendly_label(key, key), key, key,
            float(util) if util is not None else None,
            parse_resets_at(ra_raw), ra_raw, obj.get("severity"), "top-level",
        )
        k = _dedup_key(q)
        if k in seen:
            continue
        seen.add(k)
        out.append(q)

    return out


def weekly_reset(usage: dict):
    """Return ``(datetime, raw_str)`` for the WEEKLY quota's NEXT reset, or
    ``(None, None)`` if none is reported.

    Prefers the aggregate ``weekly`` quota (weekly_all / seven_day); falls back
    to the earliest weekly-scoped limit, then the top-level ``seven_day`` object.
    The datetime is tz-aware UTC (``.timestamp()`` gives the machine-local epoch).
    Used to tell a resumed agent how long until the weekly limit resets again --
    independent of WHICH quota (session/weekly) just cleared."""
    aggregate = None            # the plain "weekly" (weekly_all/seven_day)
    scoped = None               # earliest of any weekly (Opus)/(scoped)/... limit
    for lim in usage.get("limits") or []:
        label = friendly_label(lim.get("kind"), lim.get("group"))
        if not label.startswith("weekly"):
            continue
        ra = parse_resets_at(lim.get("resets_at"))
        if ra is None:
            continue
        pair = (ra, lim.get("resets_at"))
        if label == "weekly":
            aggregate = pair
        elif scoped is None or ra < scoped[0]:
            scoped = pair
    if aggregate is not None:
        return aggregate
    obj = usage.get("seven_day")
    if isinstance(obj, dict):
        ra = parse_resets_at(obj.get("resets_at"))
        if ra is not None:
            return ra, obj.get("resets_at")
    return scoped if scoped is not None else (None, None)


def is_quota_reset(usage: dict, quota: dict, below: float = RESET_CONFIRM_BELOW) -> bool:
    """True iff the given quota has been RESET in a freshly-fetched `usage`.

    Matches by friendly label (so limits[] 'session' and top-level 'five_hour'
    are the same quota). Reset means its current utilization dropped below
    `below`, or the quota is no longer reported at all. A quota still at/above
    `below` means the server has not applied the reset yet -> not reset."""
    label = quota.get("label")
    current = None
    for q in iter_quota_utilizations(usage):
        if q["label"] == label:
            current = q["percent"] if current is None else max(current, q["percent"])
    if current is None:
        return True  # quota no longer present -> treat as reset
    return current < below
