"""Key combos reach cua-driver with the modifier names its platform accepts.

Live QA (Nov 2026, Linux X11 desktop): ``key("ctrl+alt+t")`` was logged by the driver as ``ctrl+t`` and
``alt+f2`` as ``+f2``. Hermes canonicalises Alt to macOS's ``option`` (which the safety blocklist keeps
using), and the Linux driver silently drops a modifier it does not know.
"""
import pytest

from tools.computer_use import cua_backend_input
from tools.computer_use.cua_backend_input import _InputMixin
from tools.computer_use.cua_backend_parse import driver_modifiers


class _Recorder(_InputMixin):
    def __init__(self):
        self.calls = []

    def _target_args(self, action, *, need_window=False):
        return None, {"pid": 42, "window_id": 7}

    def _run_input_action(self, action, args, delivery_mode, bring_to_front):
        self.calls.append((action, args))



@pytest.mark.parametrize("platform,expected", [
    ("linux", ["ctrl", "alt", "t"]),
    ("win32", ["ctrl", "alt", "t"]),
    ("darwin", ["ctrl", "option", "t"]),
])
def test_hotkey_uses_the_driver_platforms_alt_name(monkeypatch, platform, expected):
    monkeypatch.setattr(cua_backend_input.sys, "platform", platform)
    rec = _Recorder()
    rec.key("ctrl+alt+t")
    assert rec.calls == [("hotkey", {"pid": 42, "window_id": 7, "keys": expected})]


@pytest.mark.parametrize("spelling", ["alt+f2", "option+f2", "Alt-F2"])
def test_every_alt_spelling_arrives_as_alt_on_linux(monkeypatch, spelling):
    monkeypatch.setattr(cua_backend_input.sys, "platform", "linux")
    rec = _Recorder()
    rec.key(spelling)
    assert rec.calls[0][1]["keys"] == ["alt", "f2"]


def test_other_modifiers_and_plain_keys_unchanged(monkeypatch):
    monkeypatch.setattr(cua_backend_input.sys, "platform", "linux")
    rec = _Recorder()
    rec.key("ctrl+shift+c")
    rec.key("Return")
    assert rec.calls == [("hotkey", {"pid": 42, "window_id": 7, "keys": ["ctrl", "shift", "c"]}),
                         ("press_key", {"pid": 42, "window_id": 7, "key": "return"})]
    assert driver_modifiers(["cmd", "shift"], "linux") == ["cmd", "shift"]
