"""Tests for secure Android wireless ADB pairing and connection."""

import subprocess

import pytest

from desktop.wireless_adb import WirelessAdbError, pair_and_connect, reconnect_paired_devices


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def test_pairing_code_is_stdin_not_a_process_argument():
    calls = []
    results = iter(
        [
            _completed(stdout="Successfully paired to 192.168.1.24:37123"),
            _completed(stdout="connected to 192.168.1.24:38555"),
        ]
    )

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return next(results)

    result = pair_and_connect(
        "192.168.1.24",
        "37123",
        "123456",
        "38555",
        adb_path="adb",
        runner=runner,
    )

    assert len(calls) == 2
    assert calls[0][0] == ["adb", "pair", "192.168.1.24:37123"]
    assert calls[0][1]["input"] == "123456\n"
    assert all("123456" not in str(args) for args, _ in calls)
    assert calls[1][0] == ["adb", "connect", "192.168.1.24:38555"]
    assert "connected to 192.168.1.24:38555" in result
    assert "pairing code was not saved" in result


@pytest.mark.parametrize(
    ("ip", "pairing_port", "code", "connection_port"),
    [
        ("not-an-ip", "37123", "123456", "38555"),
        ("192.168.1.24", "0", "123456", "38555"),
        ("192.168.1.24", "37123", "123456", "65536"),
        ("192.168.1.24", "37123", "abc123", "38555"),
    ],
)
def test_invalid_pairing_input_is_rejected_before_running_adb(ip, pairing_port, code, connection_port):
    calls = []

    with pytest.raises(WirelessAdbError):
        pair_and_connect(ip, pairing_port, code, connection_port, adb_path="adb", runner=lambda *a, **k: calls.append(a))

    assert calls == []


def test_pair_failure_does_not_attempt_connection_or_echo_pairing_code():
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return _completed(returncode=1, stderr="pairing failed for code 123456")

    with pytest.raises(WirelessAdbError) as exc:
        pair_and_connect(
            "192.168.1.24",
            "37123",
            "123456",
            "38555",
            adb_path="adb",
            runner=runner,
        )

    assert len(calls) == 1
    assert "123456" not in str(exc.value)


def test_connect_failure_explains_that_connection_uses_a_separate_port():
    results = iter(
        [
            _completed(stdout="Successfully paired to 192.168.1.24:37123"),
            _completed(returncode=0, stdout="failed to connect to 192.168.1.24:38555"),
        ]
    )

    with pytest.raises(WirelessAdbError, match="separate port"):
        pair_and_connect(
            "192.168.1.24",
            "37123",
            "123456",
            "38555",
            adb_path="adb",
            runner=lambda *args, **kwargs: next(results),
        )


def test_ipv6_endpoint_is_bracketed():
    calls = []
    results = iter([_completed(stdout="Successfully paired"), _completed(stdout="connected")])

    def runner(args, **kwargs):
        calls.append(args)
        return next(results)

    pair_and_connect("2001:db8::1", "37123", "123456", "38555", adb_path="adb", runner=runner)

    assert calls[0][-1] == "[2001:db8::1]:37123"
    assert calls[1][-1] == "[2001:db8::1]:38555"



def test_auto_reconnect_only_uses_paired_tls_connect_services_and_skips_active_devices():
    calls = []
    results = iter(
        [
            _completed(
                stdout=(
                    "List of discovered mdns services\n"
                    "adb-one _adb-tls-connect._tcp. 192.168.1.24:38555\n"
                    "adb-one _adb-tls-pairing._tcp 192.168.1.24:37123\n"
                    "adb-two _adb-tls-connect._tcp. 192.168.1.25:41231\n"
                    "adb-bad _adb-tls-connect._tcp. not-an-ip:41231\n"
                )
            ),
            _completed(stdout="List of devices attached\n192.168.1.24:38555 device product:phone\n"),
            _completed(stdout="connected to 192.168.1.25:41231"),
        ]
    )

    def runner(args, **kwargs):
        calls.append(args)
        return next(results)

    assert reconnect_paired_devices(adb_path="adb", runner=runner) == ["192.168.1.25:41231"]
    assert calls == [
        ["adb", "mdns", "services"],
        ["adb", "devices"],
        ["adb", "connect", "192.168.1.25:41231"],
    ]


def test_auto_reconnect_returns_empty_when_no_connect_service_is_advertised():
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        return _completed(stdout="adb-one _adb-tls-pairing._tcp 192.168.1.24:37123")

    assert reconnect_paired_devices(adb_path="adb", runner=runner) == []
    assert calls == [["adb", "mdns", "services"]]


def test_auto_reconnect_does_not_connect_after_adb_discovery_failure():
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        return _completed(returncode=1, stderr="failure")

    with pytest.raises(WirelessAdbError, match="could not discover"):
        reconnect_paired_devices(adb_path="adb", runner=runner)
    assert calls == [["adb", "mdns", "services"]]
