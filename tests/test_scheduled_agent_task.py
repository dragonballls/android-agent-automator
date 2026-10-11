"""Unit tests for saved natural-language task payloads and scheduler dispatch."""
import base64
import json
import sys

import pytest

from gitd.services.scheduled_agent_task import build_execution_prompt, decode_payload
from gitd.services.multi_device_tasks import build_device_schedule_payloads
from gitd.services.job_engine import _build_scheduled_cmd, _skill_config_preflight


def _encoded(payload):
    return base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def test_saved_task_payload_accepts_daily_agent_task():
    payload = {
        "prompt": "Open Settings and report the current Wi-Fi status",
        "provider": "gemini",
        "model": "gemini-3.8-flash",
        "device": "serial-123",
    }

    assert decode_payload(_encoded(payload)) == payload


@pytest.mark.parametrize("payload", [
    {"prompt": "", "device": "serial-123"},
    {"prompt": "do it", "device": ""},
    {"prompt": "x" * 5001, "device": "serial-123"},
])
def test_saved_task_payload_rejects_missing_or_oversized_fields(payload):
    with pytest.raises(ValueError):
        decode_payload(_encoded(payload))


def test_agent_task_preflight_requires_prompt_and_supported_provider():
    assert _skill_config_preflight("agent_task", {"prompt": "Check the weather", "provider": "gemini"}) is None
    assert "config.prompt" in _skill_config_preflight("agent_task", {"provider": "gemini"})
    assert "supported agent-chat provider" in _skill_config_preflight(
        "agent_task", {"prompt": "Do a task", "provider": "unknown-provider"}
    )


def test_scheduler_builds_agent_task_command_with_saved_config(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    config = {
        "prompt": "Open Maps and report the current screen",
        "provider": "gemini",
        "model": "gemini-3.8-flash",
    }
    command = _build_scheduled_cmd("agent_task", config, "serial-123")
    assert command[0] == sys.executable
    assert command[1] == "-u"
    assert command[2].endswith("scheduled_agent_task.py")
    decoded = decode_payload(command[3])
    assert decoded == {
        "prompt": config["prompt"],
        "provider": "gemini",
        "model": "gemini-3.8-flash",
        "device": "serial-123",
    }


def test_saved_task_prompt_repeats_until_verified_completion_then_advances():
    prompt = build_execution_prompt("Repeat the safe setup steps until complete, then open the next page.")

    assert "do not stop after merely attempting the first action" in prompt
    assert "repeat the authorized action while the observed screen shows the goal is still incomplete" in prompt
    assert "finish and verify each stage before moving to the next stage" in prompt
    assert "three consecutive cycles with no measurable progress" in prompt
    assert "Do not automate rewarded-ad viewing/clicking" in prompt
    assert "Repeat the safe setup steps until complete, then open the next page." in prompt

def test_multi_device_schedules_create_one_identical_daily_task_per_phone():
    payloads = build_device_schedule_payloads(
        "Daily check",
        "Open Settings and verify Wi-Fi, then report the result",
        ["pixel-serial", "moto-serial", "pixel-serial"],
        "gemini",
        "gemini-3.8-flash",
        "09:00",
    )

    assert [payload["phone_serial"] for payload in payloads] == ["pixel-serial", "moto-serial"]
    assert all(payload["job_type"] == "agent_task" for payload in payloads)
    assert all(payload["schedule_type"] == "daily" for payload in payloads)
    assert all(payload["daily_times"] == ["09:00"] for payload in payloads)
    assert all(payload["config_json"]["prompt"] == payloads[0]["config_json"]["prompt"] for payload in payloads)
    assert all(payload["config_json"]["provider"] == "gemini" for payload in payloads)


def test_multi_device_schedules_reject_empty_targets():
    with pytest.raises(ValueError, match="at least one connected phone"):
        build_device_schedule_payloads("Daily check", "Do work", [], "gemini", "model", "09:00")

