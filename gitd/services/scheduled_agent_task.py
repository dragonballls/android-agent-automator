"""Run one saved natural-language phone task from the existing scheduler.

This module is also invoked as a child process by the frozen Windows launcher.
Only the task description/provider/model/device are passed; API and device PIN
credentials remain in the OS credential vault / process environment.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any


def decode_payload(encoded: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
        value = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("Invalid saved-task payload") from exc
    if not isinstance(value, dict):
        raise ValueError("Saved-task payload must be an object")
    prompt = value.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 5000:
        raise ValueError("Saved task must contain a prompt of 1–5000 characters")
    device = value.get("device")
    if not isinstance(device, str) or not device.strip():
        raise ValueError("Saved task requires a device serial")
    return value


def run_payload(encoded: str) -> int:
    """Execute one task and stream only user-facing output/tool names to stdout."""
    try:
        config = decode_payload(encoded)
    except ValueError as exc:
        print(f"Saved task configuration error: {exc}", flush=True)
        return 2

    prompt = config["prompt"].strip()
    device = config["device"].strip()
    provider = str(config.get("provider") or "gemini").strip()
    model = str(config.get("model") or "").strip()

    try:
        from gitd.services.agent_chat import chat_turn, create_session, save_session_to_db

        session = create_session(device=device, provider=provider, model=model)
        session.auto_screenshot = True
        print(f"[saved-task] starting device={device} provider={session.provider} model={session.model}", flush=True)
        exit_code = 0
        try:
            for event in chat_turn(session, prompt):
                event_type = str(event.get("type") or "")
                if event_type == "text":
                    chunk = event.get("content") or event.get("text") or ""
                    if chunk:
                        print(str(chunk), end="", flush=True)
                elif event_type in ("tool_call", "tool_start"):
                    # Never log tool arguments: they can contain private user data.
                    name = event.get("name") or event.get("tool") or event.get("tool_name") or "device action"
                    print(f"\n[saved-task] action: {name}", flush=True)
                elif event_type == "error":
                    print(
                        f"\n[saved-task] ERROR: {event.get('content') or event.get('error') or 'unknown error'}",
                        flush=True,
                    )
                    exit_code = 1
                elif event_type == "done":
                    print("\n[saved-task] finished", flush=True)
        finally:
            try:
                save_session_to_db(session)
            except Exception:
                print("[saved-task] Warning: conversation history could not be saved.", flush=True)
        return exit_code
    except Exception as exc:
        # Do not print exception representations that could include request headers or credentials.
        print(f"Saved task failed ({type(exc).__name__}). Check launcher logs and provider configuration.", flush=True)
        return 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("Usage: scheduled_agent_task.py <base64-task-payload>", file=sys.stderr)
        return 2
    # Support execution as a script in a source checkout as well as import
    # from a PyInstaller bundle, where gitd is packaged by the build workflow.
    if __package__ in (None, ""):
        root = Path(__file__).resolve().parents[2]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
    return run_payload(args[0])


if __name__ == "__main__":
    raise SystemExit(main())
