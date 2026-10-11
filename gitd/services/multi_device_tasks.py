"""Helpers for creating the same saved automation on multiple devices."""

from __future__ import annotations

from collections.abc import Iterable


def build_device_schedule_payloads(
    name: str,
    prompt: str,
    devices: Iterable[str],
    provider: str,
    model: str,
    daily_time: str,
) -> list[dict]:
    """Build one scheduler record per distinct device, preserving target order."""
    targets: list[str] = []
    seen: set[str] = set()
    for value in devices:
        serial = str(value).strip()
        if serial and serial not in seen:
            targets.append(serial)
            seen.add(serial)
    if not targets:
        raise ValueError("Select at least one connected phone.")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 5000:
        raise ValueError("Task prompt must contain 1–5000 characters.")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Task name is required.")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("AI provider is required.")

    config = {
        "prompt": prompt.strip(),
        "provider": provider.strip(),
        "model": str(model or "").strip(),
        "source": "windows_launcher",
    }
    return [
        {
            "name": name.strip()[:100],
            "job_type": "agent_task",
            "phone_serial": serial,
            "priority": 2,
            "schedule_type": "daily",
            "daily_times": [daily_time],
            "max_duration_s": 1800,
            "is_enabled": 1,
            "config_json": dict(config),
        }
        for serial in targets
    ]
