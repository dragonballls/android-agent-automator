"""Pair Android devices over Wi-Fi through the official ADB Wireless debugging flow.

Pairing codes are submitted over stdin rather than placed in process arguments,
and are never persisted or included in returned error messages.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
from collections.abc import Callable


class WirelessAdbError(ValueError):
    """A safe, user-facing wireless ADB setup error."""


def _port(value: str | int, label: str) -> int:
    raw = str(value).strip()
    if not re.fullmatch(r"\d{1,5}", raw):
        raise WirelessAdbError(f"{label} must be a valid port number between 1 and 65535.")
    result = int(raw)
    if not 1 <= result <= 65535:
        raise WirelessAdbError(f"{label} must be a valid port number between 1 and 65535.")
    return result


def _endpoint(ip: str, port: str | int, label: str) -> str:
    try:
        address = ipaddress.ip_address(str(ip).strip())
    except ValueError as exc:
        raise WirelessAdbError(f"{label} must be a valid IPv4 or IPv6 address shown on the phone.") from exc
    port_number = _port(port, label)
    host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return f"{host}:{port_number}"


def pair_and_connect(
    ip_address: str,
    pairing_port: str | int,
    pairing_code: str,
    connection_port: str | int,
    *,
    adb_path: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> str:
    """Pair, then connect to the phone's separate Wireless debugging endpoint."""
    pair_endpoint = _endpoint(ip_address, pairing_port, "Pairing endpoint")
    connect_endpoint = _endpoint(ip_address, connection_port, "Wireless debugging endpoint")
    code = str(pairing_code).strip()
    if not re.fullmatch(r"\d{4,12}", code):
        raise WirelessAdbError("Enter the numeric pairing code currently shown by Android Wireless debugging.")

    executable = adb_path or shutil.which("adb")
    if not executable:
        raise WirelessAdbError(
            "ADB was not found. Install Android SDK Platform-Tools and make sure adb is available on PATH."
        )
    invoke = runner or subprocess.run
    options = {
        "capture_output": True,
        "text": True,
        "timeout": 30,
    }
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        paired = invoke([executable, "pair", pair_endpoint], input=code + "\n", **options)
    except subprocess.TimeoutExpired as exc:
        raise WirelessAdbError("Wireless pairing timed out. Reopen the pairing-code screen and try again.") from exc
    except OSError as exc:
        raise WirelessAdbError(
            "Could not start ADB. Check that Android SDK Platform-Tools is installed correctly."
        ) from exc
    if paired.returncode != 0:
        raise WirelessAdbError(
            "Wireless pairing failed. Recheck the pairing code and pairing port shown on the phone, then try again."
        )

    try:
        connected = invoke([executable, "connect", connect_endpoint], **options)
    except subprocess.TimeoutExpired as exc:
        raise WirelessAdbError(
            "Pairing succeeded, but connecting timed out. Recheck the IP address and the separate Wireless debugging port."
        ) from exc
    except OSError as exc:
        raise WirelessAdbError(
            "Pairing succeeded, but ADB could not start the connection. Check Android SDK Platform-Tools."
        ) from exc

    output = (connected.stdout or "") + "\n" + (connected.stderr or "")
    lower_output = output.lower()
    if connected.returncode != 0 or "failed to connect" in lower_output or "unable to connect" in lower_output:
        raise WirelessAdbError(
            "Pairing succeeded, but the connection failed. Check the current IP address and the separate port on "
            "the main Wireless debugging screen; then try Pair over Wi-Fi again."
        )

    return (
        f"Wireless ADB connected to {connect_endpoint}. Use Refresh Connected Phones to see it in Target phones. "
        "The pairing code was not saved."
    )



def _normalise_discovered_endpoint(value: str) -> str | None:
    """Validate an endpoint emitted by ADB's mDNS service discovery."""
    raw = str(value).strip()
    if raw.startswith("["):
        close = raw.find("]")
        if close < 0 or raw[close + 1:close + 2] != ":":
            return None
        host, port = raw[1:close], raw[close + 2:]
    else:
        host, separator, port = raw.rpartition(":")
        if not separator:
            return None
    try:
        return _endpoint(host, port, "Discovered ADB endpoint")
    except WirelessAdbError:
        return None


def reconnect_paired_devices(
    *, adb_path: str | None = None, runner: Callable[..., subprocess.CompletedProcess] | None = None
) -> list[str]:
    """Reconnect previously authorized Android wireless devices advertised by ADB mDNS.

    This never pairs a new device or uses a pairing code. Android must already trust
    this computer, Wireless debugging must be enabled, and the devices must be
    discoverable on the current local network.
    """
    executable = adb_path or shutil.which("adb")
    if not executable:
        raise WirelessAdbError(
            "ADB was not found. Install Android SDK Platform-Tools and make sure adb is available on PATH."
        )
    invoke = runner or subprocess.run
    options = {"capture_output": True, "text": True, "timeout": 10}
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        discovered = invoke([executable, "mdns", "services"], **options)
    except subprocess.TimeoutExpired as exc:
        raise WirelessAdbError("ADB wireless-device discovery timed out.") from exc
    except OSError as exc:
        raise WirelessAdbError("Could not start ADB for wireless-device discovery.") from exc
    if discovered.returncode != 0:
        raise WirelessAdbError("ADB could not discover wireless devices. Check the Platform-Tools installation.")

    endpoints: list[str] = []
    for line in (discovered.stdout or "").splitlines():
        fields = line.split()
        if len(fields) < 3 or "_adb-tls-connect._tcp" not in fields:
            continue
        endpoint = _normalise_discovered_endpoint(fields[-1])
        if endpoint and endpoint not in endpoints:
            endpoints.append(endpoint)
    if not endpoints:
        return []

    # Avoid reconnecting endpoints that ADB already reports as connected.
    active: set[str] = set()
    try:
        listed = invoke([executable, "devices"], **options)
        if listed.returncode == 0:
            for line in (listed.stdout or "").splitlines():
                fields = line.split()
                if len(fields) >= 2 and fields[1] == "device":
                    normalised = _normalise_discovered_endpoint(fields[0])
                    if normalised:
                        active.add(normalised)
    except (OSError, subprocess.TimeoutExpired):
        # Discovery can still make progress if the optional active-list check fails.
        pass

    newly_connected: list[str] = []
    for endpoint in endpoints:
        if endpoint in active:
            continue
        try:
            result = invoke([executable, "connect", endpoint], **options)
        except subprocess.TimeoutExpired:
            continue
        except OSError:
            continue
        output = ((result.stdout or "") + "\n" + (result.stderr or "")).lower()
        if result.returncode == 0 and "failed to connect" not in output and "unable to connect" not in output:
            newly_connected.append(endpoint)
    return newly_connected
