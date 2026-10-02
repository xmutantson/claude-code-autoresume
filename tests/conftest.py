"""Offline pytest safety: synthetic profiles and no network/process/UI effects."""
import os
import sys
import tempfile

import pytest


_profile = tempfile.TemporaryDirectory(prefix="autoresume-tests-")
tempfile.tempdir = _profile.name
for _name in ("USERPROFILE", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "CLAUDE_CONFIG_DIR"):
    os.environ[_name] = _profile.name
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _audit(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "subprocess.Popen",
                 "os.system", "os.startfile", "os.startfile/2"}:
        raise AssertionError(f"offline test attempted {event}")


sys.addaudithook(_audit)


@pytest.fixture(autouse=True)
def offline_boundaries(monkeypatch):
    import autoresume as ar
    import usage_api

    def forbidden(*args, **kwargs):
        raise AssertionError("test attempted real desktop injection")

    monkeypatch.setattr(usage_api, "detect_cli_version", lambda: usage_api.DEFAULT_CLI_VERSION)
    monkeypatch.setattr(ar, "usage_monitor_running", lambda *a: False)
    for name in ("_shell_execute", "press_vk", "type_unicode", "activate_window",
                 "focus_claude_input", "inject_via_ahk"):
        monkeypatch.setattr(ar, name, forbidden)


def pytest_sessionfinish(session, exitstatus):
    _profile.cleanup()
