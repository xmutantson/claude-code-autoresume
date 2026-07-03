#!/usr/bin/env python3
"""Tests for the v0.4.0 URI injector: target the EXACT Claude Code session tab.

The URI injector fires
  vscode://anthropic.claude-code/open?session=<ID>&prompt=<URLENCODED>
which focuses that exact tab and pre-fills the prompt (NOT auto-submitted -> one
guarded Enter submits). These tests cover:

  * build_resume_uri: session + prompt are url-encoded with quote(safe="").
  * inject_via_uri: foregrounds the right window, fires the URI (injected
    ShellExecuteW stub -- no real launch), settles, submits with ONE Enter, and
    the foreground guard HOLDS the Enter (never types into a non-VS-Code window).
  * session-id / workspace derivation from a transcript's filename + cwd field.

No real window, no real ShellExecuteW: window helpers are monkeypatched and the
shell-execute is injected. Run:  python -m pytest tests/test_uri_inject.py -q
"""

import os
import sys
from urllib.parse import unquote

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import autoresume as ar  # noqa: E402


# --------------------------------------------------------------------------- #
# build_resume_uri: URL encoding                                              #
# --------------------------------------------------------------------------- #

def test_build_resume_uri_encodes_reserved_chars():
    url = ar.build_resume_uri("4b578e73-e3ec-443e-9477-49988f19230b", "a b/c&d=e")
    assert url.startswith("vscode://anthropic.claude-code/open?session=")
    # session (a UUID) has no reserved chars -> passes through verbatim.
    assert "session=4b578e73-e3ec-443e-9477-49988f19230b" in url
    # prompt reserved chars are all percent-escaped (safe="" -> even '/').
    assert "&prompt=a%20b%2Fc%26d%3De" in url
    # no raw space / unescaped '&'/'=' leaks INTO the prompt value.
    prompt_val = url.split("&prompt=", 1)[1]
    assert " " not in prompt_val and "&" not in prompt_val


def test_build_resume_uri_roundtrips_full_resume_message():
    msg = ar.build_message("weekly", "Jul 3, 1am (America/Los_Angeles)")
    url = ar.build_resume_uri("SID-123", msg)
    prompt_val = url.split("&prompt=", 1)[1]
    # unquoting the prompt yields the original message byte-for-byte.
    assert unquote(prompt_val) == msg
    assert "session=SID-123&prompt=" in url


def test_build_resume_uri_empty_session():
    url = ar.build_resume_uri("", "hi")
    assert url == "vscode://anthropic.claude-code/open?session=&prompt=hi"


# --------------------------------------------------------------------------- #
# session-id / workspace derivation                                           #
# --------------------------------------------------------------------------- #

def test_session_id_from_path():
    assert ar.session_id_from_path(r"C:\p\4b578e73-e3ec-443e.jsonl") == \
        "4b578e73-e3ec-443e"
    assert ar.session_id_from_path("/x/y/abc.jsonl") == "abc"
    assert ar.session_id_from_path(None) is None


def test_workspace_title_from_cwd():
    assert ar.workspace_title_from_cwd(
        r"X:\Storage\Documents\hermes and mercury") == "hermes and mercury"
    assert ar.workspace_title_from_cwd(
        "X:\\repo\\") == "repo"     # trailing sep stripped
    assert ar.workspace_title_from_cwd(None) is None
    assert ar.workspace_title_from_cwd("") is None


def test_derive_session_and_workspace(tmp_path):
    wd = tmp_path / "watch"
    wd.mkdir()
    sid = "4b578e73-e3ec-443e-9477-49988f19230b"
    f = wd / f"{sid}.jsonl"
    # two lines: cwd changes mid-session; the LAST cwd wins.
    f.write_text(
        '{"type":"user","cwd":"X:\\\\a\\\\old","sessionId":"%s"}\n'
        '{"type":"assistant","cwd":"X:\\\\Storage\\\\Documents\\\\hermes and mercury",'
        '"sessionId":"%s"}\n' % (sid, sid),
        encoding="utf-8",
    )
    got_sid, got_ws = ar.derive_session_and_workspace(str(wd))
    assert got_sid == sid
    assert got_ws == "hermes and mercury"


def test_derive_session_and_workspace_empty(tmp_path):
    wd = tmp_path / "empty"
    wd.mkdir()
    assert ar.derive_session_and_workspace(str(wd)) == (None, None)


# --------------------------------------------------------------------------- #
# inject_via_uri: fixtures                                                     #
# --------------------------------------------------------------------------- #

class _SleepStub:
    """Stand-in for the module `time` so inject_via_uri's time.sleep is a no-op."""
    @staticmethod
    def sleep(_s):
        return None

    @staticmethod
    def time():
        return 0.0


@pytest.fixture
def wired(monkeypatch):
    """Wire inject_via_uri's window helpers to controllable fakes.

    Returns a dict of recorders; `fg` list controls successive foreground_matches
    return values (pop from front), default = VS Code in foreground."""
    rec = {"activated": [], "enters": [], "fired": [],
           "fg": [(True, "myrepo - Visual Studio Code")]}

    monkeypatch.setattr(ar, "time", _SleepStub)
    monkeypatch.setattr(ar, "select_target_window",
                        lambda *a, **k: (1234, "myrepo - Visual Studio Code",
                                         "sole VS Code window"))

    def _fg(*_a, **_k):
        return rec["fg"][0] if len(rec["fg"]) == 1 else rec["fg"].pop(0)
    monkeypatch.setattr(ar, "foreground_matches", _fg)
    monkeypatch.setattr(ar, "activate_window",
                        lambda h: rec["activated"].append(h))
    monkeypatch.setattr(ar, "press_vk", lambda vk: rec["enters"].append(vk))

    def _fire(url):
        rec["fired"].append(url)
        return 42            # > 32 == success
    rec["fire"] = _fire
    return rec


def test_inject_via_uri_fires_url_and_submits_one_enter(wired):
    ok, detail = ar.inject_via_uri(
        "resume now", "SID-9", "myrepo",
        shell_execute=wired["fire"], log=lambda m: None)
    assert ok is True
    # exactly one URI fired, correctly encoded + session-targeted.
    assert wired["fired"] == ["vscode://anthropic.claude-code/open?session=SID-9"
                              "&prompt=resume%20now"]
    # exactly one Enter (VK_RETURN) to submit the pre-filled prompt.
    assert wired["enters"] == [ar.VK_RETURN]


def test_inject_via_uri_no_session_aborts_without_firing(wired):
    ok, detail = ar.inject_via_uri(
        "msg", "", "myrepo", shell_execute=wired["fire"], log=lambda m: None)
    assert ok is False and detail == "no-session-id"
    assert wired["fired"] == [] and wired["enters"] == []


def test_inject_via_uri_shellexecute_failure(wired):
    ok, detail = ar.inject_via_uri(
        "msg", "SID-9", "myrepo",
        shell_execute=lambda url: 2,       # <= 32 == ShellExecute error
        log=lambda m: None)
    assert ok is False and detail.startswith("shellexecute-rc-")
    assert wired["enters"] == []           # never pressed Enter on a failed fire


def test_inject_via_uri_no_enter_leaves_prompt_prefilled(wired):
    ok, detail = ar.inject_via_uri(
        "msg", "SID-9", "myrepo", press_enter=False,
        shell_execute=wired["fire"], log=lambda m: None)
    assert ok is True and detail.startswith("prefilled")
    assert len(wired["fired"]) == 1
    assert wired["enters"] == []           # Enter suppressed


def test_inject_via_uri_guard_holds_enter_when_fg_not_vscode(wired):
    # URI fires (foreground for the pre-fire targeting is fine), but by Enter time
    # the foreground is a DIFFERENT app -> the guard must HOLD the Enter.
    wired["fg"] = [(True, "myrepo - Visual Studio Code"),   # pre-fire target ok
                   (False, "Some Other App"),               # Enter-time guard
                   (False, "Some Other App")]               # re-check after activate
    ok, detail = ar.inject_via_uri(
        "msg", "SID-9", "myrepo",
        shell_execute=wired["fire"], log=lambda m: None)
    assert ok is True and detail.startswith("prefilled-not-submitted")
    assert len(wired["fired"]) == 1
    assert wired["enters"] == []           # NEVER Enter into a non-VS-Code window


def test_inject_via_uri_extra_enter_sends_two(wired):
    ok, _ = ar.inject_via_uri(
        "msg", "SID-9", "myrepo", extra_enter=True,
        shell_execute=wired["fire"], log=lambda m: None)
    assert ok is True
    assert wired["enters"] == [ar.VK_RETURN, ar.VK_RETURN]
