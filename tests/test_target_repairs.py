"""Persisted target repairs and disabled AHK checks; no desktop effects."""
import subprocess
from pathlib import Path

import pytest

import autoresume as ar
from autoresume import inject_via_ahk as disabled_ahk
from test_reliability import bounded, live_args, seed_arm
from test_state_machine import FakeClock, SilentSource
from test_usage_api import FakeSource


def arrange(tmp_path, mode, target):
    args = live_args(tmp_path)
    clock = FakeClock(1_900_000_000, max_sleeps=4)
    if mode == "automatic":
        seed_arm(args, clock, target=target)
        source = FakeSource(reset_epoch=clock.t-1, confirm_seq=[True])
    else:
        ar.write_manual_request(args.manual_file, clock.t)
        ar.save_state({"handled": [], "manual_target": {
            "key": ar.handled_key("manual", clock.t), "target": target}}, args.state_file)
        source = SilentSource()
    return args, clock, source


UNRESOLVED = [None, {}, {"session_id": None, "workspace_title": None},
              {"session_id": "old-session"}, {"workspace_title": "old-workspace"},
              {"session_id": " ", "workspace_title": "old-workspace"}]


@pytest.mark.parametrize("mode", ["automatic", "manual"])
@pytest.mark.parametrize("target", UNRESOLVED)
def test_explicit_pair_repairs_and_persists_before_delivery(tmp_path, monkeypatch, mode, target):
    args, clock, source = arrange(tmp_path, mode, target)
    delivered = []

    def inject(message, sid, workspace, **kwargs):
        persisted = ar.load_state(args.state_file)
        binding = persisted["arm" if mode == "automatic" else "manual_target"]
        assert binding["target"] == {"session_id": sid, "workspace_title": workspace}
        delivered.append((sid, workspace))
        return True, "submitted"

    monkeypatch.setattr(ar, "inject_via_uri", inject)
    bounded(args, source, monkeypatch, clock)
    assert delivered == [("session-a", "workspace-a")]


@pytest.mark.parametrize("mode", ["automatic", "manual"])
def test_complete_target_survives_conflicting_explicit_options(tmp_path, monkeypatch, mode):
    target = {"session_id": "original-session", "workspace_title": "original-workspace"}
    args, clock, source = arrange(tmp_path, mode, target)
    delivered = []
    monkeypatch.setattr(ar, "inject_via_uri", lambda msg, sid, ws, **kw:
                        (delivered.append((sid, ws)) is None, "submitted"))
    bounded(args, source, monkeypatch, clock)
    assert delivered == [("original-session", "original-workspace")]


@pytest.mark.parametrize("mode", ["automatic", "manual"])
@pytest.mark.parametrize("explicit", [(None, None), ("session-a", None), (None, "workspace-a")])
def test_unresolved_target_requires_both_explicit_options(tmp_path, monkeypatch, mode, explicit):
    target = {"session_id": "original-session", "workspace_title": None}
    args, clock, source = arrange(tmp_path, mode, target)
    args.session_id, args.workspace_title = explicit
    delivered = []
    discovered = []

    def derive(*a, **kw):
        discovered.append(True)
        return "unrelated-session", "unrelated-workspace"

    monkeypatch.setattr(ar, "derive_session_and_workspace", derive)
    monkeypatch.setattr(ar, "inject_via_uri", lambda *a, **kw:
                        (delivered.append(True) is None, "submitted"))
    bounded(args, source, monkeypatch, clock)
    assert not delivered and not discovered
    state = ar.load_state(args.state_file)
    binding = state["arm" if mode == "automatic" else "manual_target"]
    assert binding["target"] == target
    assert not state["handled"]


def test_ahk_adapter_never_launches_a_process(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("disabled AHK adapter attempted to launch a process")
    monkeypatch.setattr(subprocess, "call", forbidden)
    messages = []
    assert disabled_ahk("resume", "existing.exe", "existing.ahk", messages.append) == (
        False, "ahk-disabled")
    assert "AHK injector disabled" in messages[0]


def test_ahk_watch_remains_unsubmitted(tmp_path, monkeypatch):
    args, clock, source = arrange(tmp_path, "automatic", {
        "session_id": "session-a", "workspace_title": "workspace-a"})
    args.injector, args.ahk_exe, args.ahk_script = "ahk", "unused.exe", "unused.ahk"
    monkeypatch.setattr(ar, "inject_via_ahk", disabled_ahk)
    bounded(args, source, monkeypatch, clock)
    state = ar.load_state(args.state_file)
    assert state["arm"] and not state["handled"]
    assert state["delivery"]["status"] == "not-submitted"
    assert state["delivery"]["detail"] == "ahk-disabled"


def test_ahk_standalone_script_only_exits_disabled():
    source = (Path(ar.__file__).parent / "inject.ahk").read_text(encoding="utf-8")
    statements = [line.strip() for line in source.splitlines()
                  if line.strip() and not line.lstrip().startswith(";")]
    assert statements == ["#Requires AutoHotkey v2.0", "ExitApp(5)"]
