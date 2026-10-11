"""Owner-authorized Android PIN unlock using the computer's OS credential vault.

The PIN never enters an LLM tool argument, chat, application log, or shell command
line. This only sends digit key events through an already-authorized ADB connection;
it does not bypass Android's lock-screen authentication.
"""
from __future__ import annotations

import re
import threading
import time

_SERVICE_NAME = "ghost-in-the-droid/android-device-pin"
_ALLOWED_BACKEND_PREFIXES = (
    "keyring.backends.windows",
    "keyring.backends.macos",
    "keyring.backends.secretservice",
    "keyring.backends.kwallet",
    "keyring.backends.kdewallet",
    "keyring.backends.libsecret",
)
_MIN_PIN_LENGTH = 4
_MAX_PIN_LENGTH = 16
_pin_attempt_blocked: set[str] = set()
_unlock_guard = threading.Lock()


def _validate_serial(serial: str) -> str:
    serial = (serial or "").strip()
    if serial.startswith("ios:") or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", serial):
        raise ValueError("A valid Android ADB device serial is required.")
    return serial


def _validate_pin(pin: str) -> str:
    if not isinstance(pin, str) or not re.fullmatch(r"\d{4,16}", pin):
        raise ValueError("Only numeric Android PINs of 4–16 digits are supported.")
    return pin


def _keyring_module():
    """Return keyring only when its active backend is a known OS credential vault."""
    try:
        import keyring
    except ImportError as exc:
        raise RuntimeError(
            "Secure credential storage is unavailable. Install the project dependencies with "
            "`pip install -e .` and use a supported OS credential vault."
        ) from exc

    try:
        backend = keyring.get_keyring()
        module_name = backend.__class__.__module__.lower()
        class_name = backend.__class__.__name__.lower()
        priority = float(getattr(backend, "priority", 0))
    except Exception as exc:
        raise RuntimeError("The OS credential vault could not be initialized.") from exc

    allowed = any(module_name == prefix or module_name.startswith(prefix + ".") for prefix in _ALLOWED_BACKEND_PREFIXES)
    rejected_name = any(word in module_name or word in class_name for word in ("plaintext", "null", "fail", "keyrings.alt"))
    if not allowed or rejected_name or priority <= 0:
        raise RuntimeError(
            "No supported secure OS credential vault is active. Configure Windows Credential Manager, "
            "macOS Keychain, or a Linux Secret Service/KWallet backend; insecure file-backed stores are refused."
        )
    return keyring


def store_pin(serial: str, pin: str) -> None:
    """Store a numeric PIN in the supported OS vault, scoped to one ADB serial."""
    serial = _validate_serial(serial)
    pin = _validate_pin(pin)
    keyring = _keyring_module()
    try:
        keyring.set_password(_SERVICE_NAME, serial, pin)
        _pin_attempt_blocked.discard(serial)
    except Exception as exc:
        raise RuntimeError("The OS credential vault could not save the PIN.") from exc


def has_stored_pin(serial: str) -> bool:
    """Return whether the supported OS vault has a PIN for this exact device serial."""
    serial = _validate_serial(serial)
    keyring = _keyring_module()
    try:
        return bool(keyring.get_password(_SERVICE_NAME, serial))
    except Exception as exc:
        raise RuntimeError("The OS credential vault could not read the device entry.") from exc


def remove_pin(serial: str) -> bool:
    """Remove an enrolled PIN; return False when this device has no saved PIN."""
    serial = _validate_serial(serial)
    keyring = _keyring_module()
    try:
        existing = keyring.get_password(_SERVICE_NAME, serial)
        if not existing:
            return False
        keyring.delete_password(_SERVICE_NAME, serial)
        _pin_attempt_blocked.discard(serial)
        return True
    except Exception as exc:
        raise RuntimeError("The OS credential vault could not remove the device entry.") from exc


def _read_keyguard_state(dev) -> bool | None:
    """Return True when locked, False when unlocked, or None when uncertain."""
    for args in (
        ("shell", "dumpsys", "window", "policy"),
        ("shell", "dumpsys", "window", "windows"),
    ):
        try:
            output = dev.adb(*args, timeout=8)
        except Exception:
            continue
        # Prefer the lockscreen-specific flag when a build exposes it.
        match = re.search(r"\bmShowingLockscreen\s*=\s*(true|false)\b", output, re.I)
        if match:
            return match.group(1).lower() == "true"
        match = re.search(r"\bmKeyguardShowing\s*=\s*(true|false)\b", output, re.I)
        if match:
            return match.group(1).lower() == "true"
    return None


def _screen_size(dev) -> tuple[int, int]:
    try:
        output = dev.adb("shell", "wm", "size", timeout=5)
        sizes = re.findall(r"\b(\d{2,5})x(\d{2,5})\b", output)
        if sizes:
            width, height = (int(value) for value in sizes[-1])
            if width >= 200 and height >= 400:
                return width, height
    except Exception:
        pass
    return 1080, 2400


def unlock_device(device: str) -> str:
    """Serialize unlock attempts so concurrent model tool calls cannot double-enter a PIN."""
    if not _unlock_guard.acquire(blocking=False):
        return "ERROR: An unlock operation is already running. Wait for its result before requesting another."
    try:
        return _unlock_device_once(device)
    finally:
        _unlock_guard.release()


def _unlock_device_once(device: str) -> str:
    """Use an enrolled local PIN for a single Android unlock attempt, then verify."""
    try:
        serial = _validate_serial(device)
    except ValueError as exc:
        return f"ERROR: {exc}"

    # Import the ADB client only inside the operation, keeping tests and status checks lightweight.
    from gitd.bots.common.adb import Device

    dev = Device(serial)
    try:
        # Wake first so an asleep phone exposes its actual keyguard state.
        dev.adb("shell", "input", "keyevent", "KEYCODE_WAKEUP", timeout=5)
        time.sleep(0.2)
        locked = _read_keyguard_state(dev)
    except Exception:
        locked = None
    if locked is False:
        return "Device is already unlocked; no PIN was used."
    if locked is None:
        return (
            "ERROR: Could not reliably detect this device's lock state. No PIN was entered. "
            "Check the authorized ADB connection and try again."
        )
    if serial in _pin_attempt_blocked:
        return (
            "ERROR: A previous PIN attempt for this device did not verify. No new attempt will be made. "
            "After checking the PIN, re-enroll it locally with `ghost unlock-pin set <device-serial>` "
            "to explicitly authorize another attempt."
        )

    try:
        keyring = _keyring_module()
        pin = keyring.get_password(_SERVICE_NAME, serial)
        if not pin:
            return (
                "ERROR: No PIN is enrolled for this device. On the computer running the agent, run "
                "`android-agent unlock-pin set <device-serial>` to store it in the OS credential vault."
            )
        _validate_pin(pin)
    except Exception as exc:
        return f"ERROR: Secure PIN access failed: {exc}"

    try:
        width, height = _screen_size(dev)
        center_x = width // 2
        dev.adb(
            "shell", "input", "swipe",
            str(center_x), str(int(height * 0.82)),
            str(center_x), str(int(height * 0.24)), "300",
            timeout=5,
        )
        time.sleep(0.4)
        # Latch the device before entering any digits. A failed or uncertain
        # attempt requires explicit local re-enrollment before another attempt.
        _pin_attempt_blocked.add(serial)
        # Send each digit as a separate key event: never place the PIN in an
        # ADB command argument, log line, agent message, or tool result.
        for digit in pin:
            dev.adb("shell", "input", "keyevent", f"KEYCODE_{digit}", timeout=5)
        dev.adb("shell", "input", "keyevent", "KEYCODE_ENTER", timeout=5)
        time.sleep(0.6)
    except Exception:
        return (
            "ERROR: ADB input failed during the unlock attempt. The PIN was not returned or logged. "
            "Check the phone and ADB connection manually; automatic retries are disabled."
        )

    locked_after = _read_keyguard_state(dev)
    if locked_after is False:
        _pin_attempt_blocked.discard(serial)
        return "Unlocked successfully and verified through Android's reported lock state."
    if locked_after is True:
        return (
            "Unlock failed: Android still reports the phone locked. No second attempt was made. "
            "Check the enrolled PIN and device keypad, then unlock manually if needed."
        )
    return (
        "Unlock was attempted, but this Android build did not expose a reliable lock-state result. "
        "Check the phone directly before continuing; success was not assumed."
    )
