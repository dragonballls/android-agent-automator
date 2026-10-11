"""Direct Gemini provider integration tests (mocked; no real API key required)."""

import json
from types import SimpleNamespace

import pytest

from gitd.services import agent_chat


def _message(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _response(message):
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _tool_call(call_id, name, args):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


@pytest.fixture()
def stub_device(monkeypatch):
    monkeypatch.setattr(agent_chat, "get_screen_tree", lambda _device: "")
    monkeypatch.setattr(agent_chat, "get_phone_state", lambda _device: {"currentApp": "test.app"})
    monkeypatch.setattr(agent_chat, "openai_tools_for_device", lambda _device: [])
    monkeypatch.setattr(agent_chat, "system_prompt_for_device", lambda _device: "test system")
    monkeypatch.setattr(agent_chat, "_dispatch_tool", lambda _session, name, args: f"ran {name}")


def _install_fake_openai(monkeypatch, responses, recorder):
    sequence = iter(responses)

    class FakeClient:
        def __init__(self, **kwargs):
            recorder["client"] = kwargs
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=lambda **kwargs: _record_create(recorder, sequence, kwargs))
            )

    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeClient)


def _record_create(recorder, sequence, kwargs):
    recorder.setdefault("requests", []).append(kwargs)
    return next(sequence)


def test_gemini_provider_is_exposed_with_current_model_ids():
    provider = agent_chat.PROVIDERS["gemini"]
    assert provider["label"] == "Google Gemini API"
    assert provider["models"][0] == "gemini-3.8-flash"
    assert "gemini-3.1-pro-preview" in provider["models"]


def test_gemini_uses_official_compatible_endpoint_and_multiturn_tools(stub_device, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    recorder = {}
    _install_fake_openai(
        monkeypatch,
        [
            _response(_message(tool_calls=[_tool_call("c1", "tap", {"x": 3, "y": 5})])),
            _response(_message(content="Finished", tool_calls=None)),
        ],
        recorder,
    )

    session = agent_chat.ChatSession(
        id="gemini1", device="test-device", provider="gemini", model="gemini-3.8-flash"
    )
    events = list(agent_chat.chat_turn(session, "open the app and tap"))
    assert recorder["client"]["api_key"] == "test-gemini-key"
    assert recorder["client"]["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert len(recorder["requests"]) == 2
    assert recorder["requests"][0]["model"] == "gemini-3.8-flash"
    assert any(event["type"] == "tool_call" and event["name"] == "tap" for event in events)
    assert any(event["type"] == "tool_result" and event["result"] == "ran tap" for event in events)
    assert any(event["type"] == "text" and event["content"] == "Finished" for event in events)
    assert events[-1]["type"] == "done"


def test_gemini_key_falls_back_to_app_settings(stub_device, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    from gitd.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "configured-in-settings")
    recorder = {}
    _install_fake_openai(monkeypatch, [_response(_message(content="Ready"))], recorder)
    session = agent_chat.ChatSession(id="gemini2", device="test-device", provider="gemini")

    events = list(agent_chat.chat_turn(session, "hello"))
    assert recorder["client"]["api_key"] == "configured-in-settings"
    assert any(event["type"] == "text" and event["content"] == "Ready" for event in events)


def test_gemini_without_key_returns_actionable_error(stub_device, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    from gitd.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "")
    session = agent_chat.ChatSession(id="gemini3", device="test-device", provider="gemini")
    events = list(agent_chat.chat_turn(session, "hello"))

    assert events[0]["type"] == "error"
    assert "GEMINI_API_KEY" in events[0]["content"]
    assert events[-1]["type"] == "done"
