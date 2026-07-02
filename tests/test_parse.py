#!/usr/bin/env python3
"""Unit tests for the limit-hit parse: KIND + reset-time + tz math + dedup.

Runs against the byte-faithful fixture (tests/fixtures/sample_limit_hits.jsonl)
built from the owner's REAL transcript lines (session 12am, weekly Jul 3 / Jun 12,
monthly-spend), plus negatives (transient overload, normal assistant line).

Deterministic: reset times are resolved relative to a fixed `now`, and asserted
against absolute UTC instants (machine-tz independent).

Run:  python tests/test_parse.py
"""

import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # import the package module

from autoresume import (  # noqa: E402
    extract_limit_text,
    parse_limit_text,
    resolve_reset_epoch,
    handled_key,
    build_message,
)

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

LA = ZoneInfo("America/Los_Angeles") if ZoneInfo else None
FIXTURE = os.path.join(HERE, "fixtures", "sample_limit_hits.jsonl")

_passed = 0
_failed = 0


def check(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {msg}")
    else:
        _failed += 1
        print(f"  FAIL  {msg}")


def load_lines(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def main():
    print(f"Fixture: {FIXTURE}")
    objs = load_lines(FIXTURE)

    # Collect the genuine limit-hit lines via the full match predicate.
    hits = []
    for o in objs:
        t = extract_limit_text(o)
        if t:
            hits.append((t, parse_limit_text(t)))

    print("\n[1] Match predicate isolates real limit-hits from negatives")
    # 5 positives in the fixture: session, weekly x2 (Jul3 + its dup), Jun12,
    # monthly-spend. 3 negatives must be rejected.
    check(len(hits) == 5, f"exactly 5 limit-hit lines matched (got {len(hits)})")
    texts = [h[0] for h in hits]
    check(all("You've hit your" in t for t in texts), "all matched start with the prefix")
    check(not any("Server is temporarily" in t for t in texts),
          "transient-overload line rejected")
    check(not any(t == "529 Overloaded" for t in texts), "'529 Overloaded' rejected")
    check(not any("nail on the head" in t for t in texts),
          "normal assistant line rejected")

    print("\n[2] KIND extraction")
    kinds = sorted(p["kind"] for _, p in hits)
    check(kinds.count("session") == 1, "one session")
    check(kinds.count("weekly") == 3, "three weekly (Jul3, its dup, Jun12)")
    check(kinds.count("monthly spend") == 1, "one monthly spend")

    print("\n[3] SESSION reset: '12am (America/Los_Angeles)', time-only -> next occurrence")
    sess = next(p for _, p in hits if p["kind"] == "session")
    check(sess["reset_str"] == "12am (America/Los_Angeles)",
          f"reset_str exact: {sess['reset_str']!r}")
    # now = 2026-06-06 23:55 PDT (== 2026-06-07T06:55Z) -> next 12am = 06-07 00:00 PDT
    now = datetime(2026, 6, 7, 6, 55, tzinfo=timezone.utc)
    epoch, dt = resolve_reset_epoch(sess["reset_str"], now=now)
    expected = datetime(2026, 6, 7, 0, 0, tzinfo=LA)
    check(dt == expected, f"resolved {dt.isoformat()} == expected {expected.isoformat()}")
    check(int(epoch) == int(expected.timestamp()), "epoch matches absolute instant")

    print("\n[4] WEEKLY reset: 'Jul 3, 1am' -> 2026-07-03 01:00 PDT")
    wk = next(p for _, p in hits if p["kind"] == "weekly"
              and p["reset_str"].startswith("Jul 3"))
    check(wk["reset_str"] == "Jul 3, 1am (America/Los_Angeles)",
          f"reset_str exact: {wk['reset_str']!r}")
    now = datetime(2026, 7, 1, 6, 57, tzinfo=timezone.utc)
    epoch, dt = resolve_reset_epoch(wk["reset_str"], now=now)
    expected = datetime(2026, 7, 3, 1, 0, tzinfo=LA)
    check(dt == expected, f"resolved {dt.isoformat()} == expected {expected.isoformat()}")

    print("\n[5] WEEKLY reset: 'Jun 12, 1am' -> 2026-06-12 01:00 PDT")
    wk2 = next(p for _, p in hits if p["kind"] == "weekly"
               and p["reset_str"].startswith("Jun 12"))
    now = datetime(2026, 6, 9, 10, 51, tzinfo=timezone.utc)
    epoch, dt = resolve_reset_epoch(wk2["reset_str"], now=now)
    expected = datetime(2026, 6, 12, 1, 0, tzinfo=LA)
    check(dt == expected, f"resolved {dt.isoformat()} == expected {expected.isoformat()}")

    print("\n[6] WEEKLY year inference across year boundary")
    # now = late Dec 2026, reset 'Jan 2, 1am' -> should infer 2027
    now = datetime(2026, 12, 30, 12, 0, tzinfo=timezone.utc)
    epoch, dt = resolve_reset_epoch("Jan 2, 1am (America/Los_Angeles)", now=now)
    check(dt.year == 2027, f"Jan 2 from late-Dec-2026 -> {dt.year} (want 2027)")

    print("\n[7] MONTHLY-SPEND: no timed reset -> must be flagged, never injected")
    ms = next(p for _, p in hits if p["kind"] == "monthly spend")
    check(ms["reset_str"] is None, "monthly-spend reset_str is None (billing cap)")

    print("\n[8] Retry-storm dedup: identical (KIND, reset) -> identical key")
    now = datetime(2026, 7, 1, 6, 57, tzinfo=timezone.utc)
    dups = [p for _, p in hits if p["kind"] == "weekly" and p["reset_str"].startswith("Jul 3")]
    check(len(dups) == 2, f"two Jul-3 weekly lines present (got {len(dups)})")
    keys = set()
    for p in dups:
        e, _ = resolve_reset_epoch(p["reset_str"], now=now)
        keys.add(handled_key("weekly", e))
    check(len(keys) == 1, f"both dedup to ONE key: {keys}")

    print("\n[9] Timezone honored even if it differs from machine tz")
    # A UTC reset time should resolve differently from an LA one.
    e_la, d_la = resolve_reset_epoch("1am (America/Los_Angeles)",
                                     now=datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc))
    e_ny, d_ny = resolve_reset_epoch("1am (America/New_York)",
                                     now=datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc))
    check(int(e_la) != int(e_ny), "LA 1am and NY 1am resolve to different instants")

    print("\n[10] Resume message content (session & weekly)")
    msg = build_message("weekly", "Jul 3, 1am (America/Los_Angeles)")
    check(msg.startswith("[AUTOMATED RESUME]"), "starts with [AUTOMATED RESUME]")
    check("weekly usage limit was hit and has now reset" in msg, "states KIND + what happened")
    check("Jul 3, 1am (America/Los_Angeles)" in msg, "includes exact reset string")
    check("will NOT see or respond" in msg, "states user is away / will not see output")
    check("AUTONOMOUS_WORKLOG.md" in msg and "todo" in msg, "points to worklog + todos")
    check("Do not wait for user input." in msg, "tells agent not to wait")
    check("\n" not in msg, "single line (no embedded newline)")
    check(all(ord(c) < 128 for c in msg), "pure ASCII (safe to type)")

    print(f"\n==== {_passed} passed, {_failed} failed ====")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
