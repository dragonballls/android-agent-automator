"""Security and state-verification tests for owner-authorized Android PIN unlock."""
import sys
import types

import pytest

import gitd.services.device_unlock as unlock


def _fake_keyring(monkeypatch, pin="1234", backend_module="keyring.backends.Windows"):
    values = {} if pin is None else {(unlock._SERVICE_NAME, "serial-123"): pin}

    class Backend:
        priority = 1

    Backend.__module__ = backend_module
    module = types.ModuleType("keyring")
    module.get_keyring = lambda: Backend()
    setattr(module, "set_password", lambda service, user, value: values.__setitem__((service, user), value))
    setattr(module, "get_password", lambda service, user: values.get((service, user)))

    def delete_password(service, user):
        del values[(service, user)]

    setattr(module, "delete_password", delete_password)
    monkeypatch.setitem(sys.modules, "keyring", module)
    return values


def test_pin_validation_happens_before_keyring_access():
    with pytest.raises(ValueError, match="numeric Android PIN"):
        unlock.store_pin("serial-123", "not-a-pin")
    with pytest.raises(ValueError, match="numeric Android PIN"):
        unlock.store_pin("serial-123", "123")


def test_pin_is_scoped_to_device_and_removed_from_keyring(monkeypatch):
    values = _fake_keyring(monkeypatch, pin=None)

    unlock.store_pin("serial-123", "1234")

    assert values[(unlock._SERVICE_NAME, "serial-123")] == "1234"
    assert unlock.has_stored_pin("serial-123") is True
    assert unlock.remove_pin("serial-123") is True
    assert unlock.has_stored_pin("serial-123") is False


def test_insecure_file_backends_are_refused(monkeypatch):
    _fake_keyring(monkeypatch, backend_module="keyrings.alt.file")
    with pytest.raises(RuntimeError, match="secure OS credential vault"):
        unlock._keyring_module()


def test_unlock_sends_individual_keyevents_and_verifies_state(monkeypatch):
    pin = "1234"
    _fake_keyring(monkeypatch, pin=pin)
    monkeypatch.setattr(unlock.time, "sleep", lambda _: None)
    states = iter(["mShowingLockscreen=true", "mShowingLockscreen=false"])
    commands = []

    class FakeDevice:
        def __init__(self, serial):
            assert serial == "serial-123"

        def adb(self, *args, timeout=30):
            commands.append(args)
            if args == ("shell", "dumpsys", "window", "policy"):
                return next(states)
            if args == ("shell", "wm", "size"):
                return "Physical size: 1080x2400"
            return ""

    monkeypatch.setattr("gitd.bots.common.adb.Device", FakeDevice)
    result = unlock.unlock_device("serial-123")

    assert "Unlocked successfully and verified" in result
    keyevents = [
        args[-1]
        for args in commands
        if len(args) >= 4 and args[:3] == ("shell", "input", "keyevent")
    ]
    assert keyevents == [
        "KEYCODE_WAKEUP",
        "KEYCODE_1",
        "KEYCODE_2",
        "KEYCODE_3",
        "KEYCODE_4",
        "KEYCODE_ENTER",
    ]
    # The complete PIN must never be passed to ADB as a command argument.
    assert all(pin not in str(arg) for call in commands for arg in call)


def test_failed_unlock_does_not_retry_automatically(monkeypatch):
    pin = "1234"
    _fake_keyring(monkeypatch, pin=pin)
    monkeypatch.setattr(unlock.time, "sleep", lambda _: None)
    states = iter([
        "mShowingLockscreen=true",
        "mShowingLockscreen=true",
        "mShowingLockscreen=true",
    ])
    commands = []

    class FakeDevice:
        def __init__(self, serial):
            pass

        def adb(self, *args, timeout=30):
            commands.append(args)
            if args == ("shell", "dumpsys", "window", "policy"):
                return next(states)
            if args == ("shell", "wm", "size"):
                return "Physical size: 1080x2400"
            return ""

    monkeypatch.setattr("gitd.bots.common.adb.Device", FakeDevice)
    first = unlock.unlock_device("serial-123")
    pin_events_after_first = sum(
        1
        for call in commands
        if len(call) >= 4
        and call[:3] == ("shell", "input", "keyevent")
        and call[-1] != "KEYCODE_WAKEUP"
    )
    second = unlock.unlock_device("serial-123")
    pin_events_after_second = sum(
        1
        for call in commands
        if len(call) >= 4
        and call[:3] == ("shell", "input", "keyevent")
        and call[-1] != "KEYCODE_WAKEUP"
    )

    assert "still reports the phone locked" in first
    assert "previous PIN attempt" in second
    assert pin_events_after_second == pin_events_after_first


def test_unlock_device_tool_is_android_only():
    from gitd.services.agent_tools import tools_for_device
    from gitd.services.tool_platforms import supports_platform

    assert "unlock_device" in {tool["name"] for tool in tools_for_device("serial-123")}
    assert "unlock_device" not in {tool["name"] for tool in tools_for_device("ios:demo")}
    assert supports_platform("unlock_device", "android") is True
    assert supports_platform("unlock_device", "ios") is False
