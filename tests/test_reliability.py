"""Regression tests for timing, routing, delivery recovery and state isolation."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import autoresume as ar
import usage_api
from test_state_machine import _make_args, _sample, FakeClock, StopLoop, SilentSource
from test_usage_api import FakeSource, FakeClient


def bounded(args, source, monkeypatch, clock, shared=None, public=True):
    monkeypatch.setattr(ar, "time", clock)
    with pytest.raises(StopLoop):
        (ar.run_watch if public else ar._run_watch)(args, source=source, shared=shared)


def live_args(tmp_path, **kwargs):
    return _make_args(tmp_path, dry_run=False, session_id="session-a",
                      workspace_title="workspace-a", **kwargs)


def seed_arm(args, clock, **kwargs):
    pending = dict(kind="session", reset_str="reported", reset_epoch=clock.t-1,
                   fire_at=clock.t-1, key=ar.handled_key("session", clock.t-1),
                   meta={"label": "session"}, target={"session_id": "session-a",
                   "workspace_title": "workspace-a"})
    pending.update(kwargs)
    ar.save_arm({"handled": []}, pending, args.state_file)
    return pending


def test_transcript_source_waits_through_buffer(tmp_path, monkeypatch):
    args = _make_args(tmp_path)
    clock = FakeClock(1_900_000_000, max_sleeps=35)
    src = ar.TranscriptSource(args, lambda m: None)
    reset = clock.t + 20
    hits = [{"kind": "session", "reset_str": "reported", "reset_epoch": reset}]
    # Use a real source confirmation, with one synthetic observed transcript hit.
    calls = [0]
    def poll():
        calls[0] += 1
        return hits if calls[0] == 1 else []
    monkeypatch.setattr(src, "poll", poll)
    observed = []
    monkeypatch.setattr(ar, "log_event", lambda message, *a: observed.append((clock.t, message)))
    bounded(args, src, monkeypatch, clock)
    fires = [t for t, message in observed if "DRY-RUN would inject" in message]
    assert fires == [reset + args.buffer]
    assert not any("EARLY RESET" in m for _, m in observed)


def test_armed_retry_after_respects_full_deadline(tmp_path, monkeypatch):
    args = _make_args(tmp_path, usage_poll=2, confirm_interval=2)
    clock = FakeClock(1_900_000_000, max_sleeps=650)
    monkeypatch.setattr(ar, "time", clock)
    times = []
    class Client:
        def fetch(self):
            times.append(clock.t)
            if len(times) == 1:
                raise usage_api.RateLimited(600)
            return _sample(100, "exhausted")
    src = ar.UsageApiSource(args, lambda m: None, client=Client())
    seed_arm(args, clock)
    bounded(args, src, monkeypatch, clock)
    assert len(times) >= 2
    assert times[1] - times[0] == 600


def test_confirm_also_honors_idle_rate_limit(tmp_path, monkeypatch):
    clock = FakeClock(1_900_000_000)
    monkeypatch.setattr(ar, "time", clock)
    client = FakeClient([usage_api.RateLimited(600), _sample(0, "normal")])
    src = ar.UsageApiSource(_make_args(tmp_path), lambda m: None, client=client)
    src.poll()
    clock.t += 599
    assert not src.confirm_reset({"meta": {"label": "session"}}, lambda m: None)[0]
    assert client.calls == 1


@pytest.mark.parametrize("payload", [
    {}, {"limits": []}, {"five_hour": {"utilization": "bad"}},
    {"five_hour": {"utilization": float("nan")}},
    {"five_hour": {"utilization": float("inf")}},
    {"five_hour": {"utilization": -1}},
    {"limits": [{"kind": "session", "severity": "exhausted"}]},
    [], {"limits": [None]},
])
def test_unknown_quota_data_cannot_confirm(tmp_path, payload):
    src = ar.UsageApiSource(_make_args(tmp_path), lambda m: None, client=FakeClient([payload]))
    assert not src.confirm_reset({"meta": {"label": "session"}}, lambda m: None)[0]


def test_all_blockers_and_missing_previous_quota_hold(tmp_path):
    payloads = [_sample(5, "normal", 100, "exhausted"),
                {"five_hour": {"utilization": 5}},
                _sample(5, "normal", 5, "normal")]
    src = ar.UsageApiSource(_make_args(tmp_path), lambda m: None, client=FakeClient(payloads))
    pending = {"meta": {"label": "session"}, "quota_labels": ["session", "weekly"]}
    assert not src.confirm_reset(pending, lambda m: None)[0]
    assert "weekly" in pending["blocked_labels"]
    assert not src.confirm_reset(pending, lambda m: None)[0]
    assert src.confirm_reset(pending, lambda m: None)[0]


def test_selected_hwnd_not_generic_vscode_is_required(monkeypatch):
    monkeypatch.setattr(ar, "user32", SimpleNamespace(GetForegroundWindow=lambda: 22))
    monkeypatch.setattr(ar, "_window_title", lambda hwnd: "Visual Studio Code")
    monkeypatch.setattr(ar, "_window_pid", lambda hwnd: 99)
    monkeypatch.setattr(ar, "_proc_image_basename", lambda pid: "Code.exe")
    assert ar.foreground_matches(hwnd=22)[0]
    assert not ar.foreground_matches(hwnd=11)[0]


@pytest.mark.parametrize("matches", [[], [(1, "other - Visual Studio Code")],
    [(1, "repo - Visual Studio Code"), (2, "repo - Visual Studio Code")]])
def test_workspace_missing_or_ambiguous_holds(monkeypatch, matches):
    monkeypatch.setattr(ar, "_enum_target_windows", lambda *a: matches)
    assert ar.select_target_window(prefer_substr="repo")[0] is None


def test_uri_activation_failure_never_opens_link(monkeypatch):
    monkeypatch.setattr(ar, "select_target_window", lambda *a, **k: (11, "repo - Visual Studio Code", "selected"))
    monkeypatch.setattr(ar, "foreground_matches", lambda *a, **k: (False, "other"))
    monkeypatch.setattr(ar, "activate_window", lambda hwnd: None)
    monkeypatch.setattr(ar, "time", FakeClock(100))
    fired = []
    ok, _ = ar.inject_via_uri("resume", "session", "repo", shell_execute=lambda url: fired.append(url))
    assert not ok and not fired


def test_armed_target_survives_newest_transcript_changes(tmp_path, monkeypatch):
    args = live_args(tmp_path)
    args.session_id = args.workspace_title = None
    clock = FakeClock(1_900_000_000, max_sleeps=8)
    src = FakeSource(reset_epoch=clock.t+2, confirm_seq=[False, True])
    targets = iter([("original", "workspace"), ("new", "other")])
    monkeypatch.setattr(ar, "derive_session_and_workspace", lambda *a, **k: next(targets))
    delivered = []
    monkeypatch.setattr(ar, "inject_via_uri", lambda msg, sid, ws, **kw: (delivered.append((sid,ws)) is None, "submitted"))
    bounded(args, src, monkeypatch, clock)
    assert delivered == [("original", "workspace")]
    assert ar.load_state(args.state_file)["delivery"]["target"]["session_id"] == "original"


@pytest.mark.parametrize("outcome", ["prefilled-not-submitted:focus", "crash"])
def test_partial_or_uncertain_delivery_is_not_automatically_repeated(tmp_path, monkeypatch, outcome):
    args = live_args(tmp_path)
    clock = FakeClock(1_900_000_000, max_sleeps=10)
    pending = seed_arm(args, clock)
    calls = []
    def inject(*a, **kw):
        calls.append(True)
        if outcome == "crash":
            raise RuntimeError("simulated interruption")
        return False, outcome
    monkeypatch.setattr(ar, "inject_via_uri", inject)
    src = FakeSource(reset_epoch=clock.t-1, confirm_seq=[True])
    if outcome == "crash":
        monkeypatch.setattr(ar, "time", clock)
        with pytest.raises(RuntimeError, match="simulated"):
            ar.run_watch(args, source=src)
    else:
        bounded(args, src, monkeypatch, clock)
    clock.sleeps = 0
    shared = ar.WatchStatus()
    bounded(args, src, monkeypatch, clock, shared=shared)
    state = ar.load_state(args.state_file)
    assert len(calls) == 1
    assert state["arm"]["key"] == pending["key"]
    assert not ar.is_handled(state, pending["key"])
    assert shared.snapshot()["state"] == "ATTENTION"


def test_cancel_survives_poll_and_restart(tmp_path, monkeypatch):
    args = live_args(tmp_path)
    clock = FakeClock(1_900_000_000, max_sleeps=5)
    pending = seed_arm(args, clock, reset_epoch=clock.t+3600, fire_at=clock.t+3605,
                       key=ar.handled_key("session", clock.t+3600))
    src = FakeSource(reset_epoch=clock.t+3600, confirm_seq=[True])
    shared = ar.WatchStatus()
    shared.request_cancel()
    bounded(args, src, monkeypatch, clock, shared=shared)
    clock.sleeps = 0
    bounded(args, src, monkeypatch, clock)
    state = ar.load_state(args.state_file)
    assert pending["key"] in state["cancelled"]
    assert state["arm"] is None


def test_manual_journal_can_be_cancelled_without_auto_arm(tmp_path, monkeypatch):
    args = live_args(tmp_path)
    clock = FakeClock(1_900_000_000, max_sleeps=3)
    key = ar.handled_key("manual", clock.t)
    ar.write_manual_request(args.manual_file, clock.t)
    ar.save_state({"delivery": {"key": key, "status": "attempting"}}, args.state_file)
    shared = ar.WatchStatus()
    shared.request_cancel()
    bounded(args, SilentSource(), monkeypatch, clock, shared=shared)
    assert ar.load_state(args.state_file)["delivery"] is None
    assert not Path(args.manual_file).exists()


def test_dry_run_preserves_state_and_manual_request(tmp_path, monkeypatch):
    args = _make_args(tmp_path)
    clock = FakeClock(1_900_000_000, max_sleeps=5)
    seed_arm(args, clock)
    ar.write_manual_request(args.manual_file, clock.t)
    before = {p: Path(p).read_bytes() for p in (args.state_file, args.manual_file)}
    bounded(args, SilentSource(), monkeypatch, clock)
    assert all(Path(p).read_bytes() == content for p, content in before.items())
    assert not Path(args.state_file + ".lock").exists()


def test_preview_gui_controls_use_copies(tmp_path):
    args = _make_args(tmp_path)
    Path(args.stop_file).write_text("hold")
    ar.write_manual_request(args.manual_file, 12345)
    with ar.preview_args(args) as preview:
        assert preview.stop_file != args.stop_file
        Path(preview.stop_file).unlink()
        ar.clear_manual_request(preview.manual_file)
    assert Path(args.stop_file).read_text() == "hold"
    assert ar.read_manual_request(args.manual_file)["resume_at"] == 12345


def test_one_watcher_owns_state_and_lock_releases(tmp_path):
    path = str(tmp_path / "state.json")
    with ar.watcher_lock(path):
        with pytest.raises(RuntimeError, match="another watcher"):
            with ar.watcher_lock(path):
                pytest.fail("second lock acquired")
    with ar.watcher_lock(path):
        pass


def test_corrupt_state_is_preserved(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{interrupted")
    with pytest.raises(json.JSONDecodeError):
        ar.load_state(str(path))
    assert path.read_text() == "{interrupted"


def test_gui_worker_crash_reports_error(tmp_path, monkeypatch):
    def fail(*a, **kw):
        raise RuntimeError("offline test error")
    monkeypatch.setattr(ar, "run_watch", fail)
    shared = ar.WatchStatus()
    ar._watch_thread(_make_args(tmp_path), shared)
    view = ar.derive_view(shared.snapshot(), 100)
    assert view["headline"] == "ERROR"
    assert "stopped" in view["status_line"]
    assert "offline test error" in view["status_line"]


def test_actual_cli_defaults_and_confirm_override(tmp_path):
    args = ar.build_argparser().parse_args(["watch"])
    src = ar.UsageApiSource(args, lambda m: None, client=FakeClient([]))
    assert src.normal == 180 and src.confirm_fast == 30 and src.drift_recheck == 600
    args.confirm_interval = 12
    assert ar.UsageApiSource(args, lambda m: None, client=FakeClient([])).confirm_fast == 12


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "nonsense"])
def test_invalid_retry_after_rejected(value):
    assert usage_api._parse_retry_after(value) is None


@pytest.mark.parametrize("sent", [0, 1, 2])
def test_sendinput_short_write_is_uncertain(monkeypatch, sent):
    monkeypatch.setattr(ar, "user32", SimpleNamespace(SendInput=lambda *a: sent))
    inputs = [ar._kb(ar.VK_RETURN, 0, 0), ar._kb(ar.VK_RETURN, 0, ar.KEYEVENTF_KEYUP)]
    if sent < 2:
        with pytest.raises(RuntimeError, match="delivery uncertain"):
            ar._send(inputs)
    else:
        assert ar._send(inputs) == 2


@pytest.mark.parametrize("body", [b"{invalid", b"[]", b"null"])
def test_bad_json_response_is_typed_and_sanitized(monkeypatch, body):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def read(self):
            return body
    monkeypatch.setattr(usage_api.urllib.request, "urlopen", lambda *a, **k: Response())
    with pytest.raises(usage_api.UsageAPIError) as error:
        usage_api.fetch_usage(token="synthetic-token", cli_version="0.0.0")
    assert "synthetic-token" not in str(error.value)
    assert body.decode() not in str(error.value)


def test_manual_target_binds_before_due_and_survives_restart(tmp_path, monkeypatch):
    args = live_args(tmp_path)
    args.session_id = args.workspace_title = None
    clock = FakeClock(1_900_000_000, max_sleeps=2)
    ar.write_manual_request(args.manual_file, clock.t+20)
    monkeypatch.setattr(ar, "derive_session_and_workspace", lambda *a, **k: ("original", "workspace"))
    bounded(args, SilentSource(), monkeypatch, clock)
    monkeypatch.setattr(ar, "derive_session_and_workspace", lambda *a, **k: ("new", "other"))
    delivered = []
    monkeypatch.setattr(ar, "inject_via_uri", lambda msg, sid, ws, **kw: (delivered.append((sid,ws)) is None, "submitted"))
    clock.t += 20
    clock.sleeps = 0
    bounded(args, SilentSource(), monkeypatch, clock)
    assert delivered == [("original", "workspace")]


def test_held_manual_retry_uses_same_event_and_preserves_auto_arm(tmp_path, monkeypatch):
    args = live_args(tmp_path)
    clock = FakeClock(1_900_000_000, max_sleeps=8)
    pending = seed_arm(args, clock, reset_epoch=clock.t+3600, fire_at=clock.t+3605)
    state = ar.load_state(args.state_file)
    key = ar.handled_key("manual", clock.t)
    state["delivery"] = {"key": key, "status": "attempting",
                         "target": {"session_id": "held-session", "workspace_title": "held-workspace"}}
    ar.save_state(state, args.state_file)
    ar.write_manual_request(args.manual_file, clock.t)
    delivered = []
    monkeypatch.setattr(ar, "inject_via_uri", lambda msg, sid, ws, **kw: (delivered.append((sid,ws)) is None, "submitted"))
    shared = ar.WatchStatus()
    shared.request_inject_now()
    bounded(args, SilentSource(), monkeypatch, clock, shared=shared)
    state = ar.load_state(args.state_file)
    assert delivered == [("held-session", "held-workspace")]
    assert key in state["submitted"]
    assert state["arm"]["key"] == pending["key"]
    assert not Path(args.manual_file).exists()
    assert shared.snapshot()["state"] == "PENDING"
    assert shared.snapshot()["fire_at"] == pending["fire_at"]


def test_cancel_held_manual_preserves_unrelated_auto_arm(tmp_path, monkeypatch):
    args = live_args(tmp_path)
    clock = FakeClock(1_900_000_000, max_sleeps=3)
    pending = seed_arm(args, clock)
    state = ar.load_state(args.state_file)
    manual_key = ar.handled_key("manual", clock.t)
    state["delivery"] = {"key": manual_key, "status": "prefilled"}
    ar.save_state(state, args.state_file)
    ar.write_manual_request(args.manual_file, clock.t)
    shared = ar.WatchStatus()
    shared.request_cancel()
    bounded(args, SilentSource(), monkeypatch, clock, shared=shared)
    state = ar.load_state(args.state_file)
    assert state["arm"]["key"] == pending["key"]
    assert manual_key in state["cancelled"]
    assert pending["key"] not in state["cancelled"]


def test_submitted_journal_recovery_does_not_rearm_future_reset(tmp_path, monkeypatch):
    args = live_args(tmp_path)
    clock = FakeClock(1_900_000_000, max_sleeps=5)
    reset = clock.t + 3600
    pending = seed_arm(args, clock, reset_epoch=reset, fire_at=reset+5,
                       key=ar.handled_key("session", reset))
    state = ar.load_state(args.state_file)
    state["delivery"] = {"key": pending["key"], "status": "submitted"}
    ar.save_state(state, args.state_file)
    bounded(args, FakeSource(reset_epoch=reset, confirm_seq=[True]), monkeypatch, clock)
    state = ar.load_state(args.state_file)
    assert state["arm"] is None
    assert pending["key"] in state["submitted"]


def test_legacy_arm_migrates_only_explicit_complete_target(tmp_path, monkeypatch):
    args = live_args(tmp_path)
    clock = FakeClock(1_900_000_000, max_sleeps=3)
    seed_arm(args, clock, target=None)
    delivered = []
    monkeypatch.setattr(ar, "inject_via_uri", lambda msg, sid, ws, **kw: (delivered.append((sid,ws)) is None, "submitted"))
    bounded(args, FakeSource(reset_epoch=clock.t-1, confirm_seq=[True]), monkeypatch, clock)
    assert delivered == [("session-a", "workspace-a")]


def test_gui_setup_failure_starts_no_worker(tmp_path, monkeypatch):
    class Root:
        def destroy(self):
            pass
    monkeypatch.setattr(ar, "tk", SimpleNamespace(Tk=Root, TclError=RuntimeError))
    def fail(*a, **k):
        raise ValueError("UI setup failed")
    monkeypatch.setattr(ar, "StatusWindow", fail)
    started = []
    monkeypatch.setattr(ar.threading, "Thread", lambda *a, **k: started.append(True))
    with pytest.raises(ValueError, match="UI setup failed"):
        ar.run_gui(_make_args(tmp_path))
    assert not started


def test_shutdown_during_confirmation_prevents_delivery(tmp_path, monkeypatch):
    args = live_args(tmp_path)
    clock = FakeClock(1_900_000_000, max_sleeps=3)
    seed_arm(args, clock)
    shared = ar.WatchStatus()
    src = SilentSource()
    def confirm(*a):
        shared.shutdown.set()
        return True, "clear"
    monkeypatch.setattr(src, "confirm_reset", confirm)
    monkeypatch.setattr(ar, "time", clock)
    ar.run_watch(args, source=src, shared=shared)
    assert ar.load_state(args.state_file).get("delivery") is None


def test_no_enter_command_succeeds_without_submission(tmp_path, monkeypatch):
    args = ar.build_argparser().parse_args([
        "inject-now", "preview", "--no-enter", "--session-id", "s", "--workspace-title", "w",
        "--log-file", str(tmp_path / "log.txt")])
    monkeypatch.setattr(ar, "inject_via_uri", lambda *a, **k: (False, "prefilled-not-submitted:w"))
    assert ar.cmd_inject_now(args) == 0
    args.injector = "ahk"
    assert ar.cmd_inject_now(args) == 1


def test_kill_switch_created_during_confirmation_prevents_delivery(tmp_path, monkeypatch):
    args = live_args(tmp_path)
    clock = FakeClock(1_900_000_000, max_sleeps=3)
    seed_arm(args, clock)
    src = SilentSource()
    def confirm(*a):
        Path(args.stop_file).write_text("hold")
        return True, "clear"
    monkeypatch.setattr(src, "confirm_reset", confirm)
    bounded(args, src, monkeypatch, clock)
    assert ar.load_state(args.state_file).get("delivery") is None
