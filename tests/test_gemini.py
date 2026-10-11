from types import SimpleNamespace

import pytest
from google.genai import types

from agent.gemini import (
    FunctionCall,
    GeminiClient,
    GeminiConfig,
    ModelConfigurationError,
    ModelResponseError,
    ModelUnavailableError,
    _parse_response,
    load_gemini_config,
)
from agent.trace import EventLog


def response(*, calls=(), text=""):
    parts = []
    for call in calls:
        parts.append(SimpleNamespace(function_call=SimpleNamespace(name=call.name, args=call.arguments), text=None))
    if text:
        parts.append(SimpleNamespace(function_call=None, text=text))
    return SimpleNamespace(candidates=[SimpleNamespace(
        finish_reason="STOP", content=SimpleNamespace(parts=parts)
    )], prompt_feedback=None)


def test_parse_response_preserves_gemini_function_id_and_thought_signature():
    signature = b"opaque-signature"
    model_content = types.Content(role="model", parts=[types.Part(
        function_call=types.FunctionCall(name="read_inventory", args={}, id="call-17"),
        thought_signature=signature,
    )])
    result = SimpleNamespace(candidates=[SimpleNamespace(
        finish_reason="STOP", content=model_content
    )], prompt_feedback=None)

    reply = _parse_response(result, "gemini-3.6-flash")

    assert reply.function_call == FunctionCall("read_inventory", {}, "call-17")
    assert reply.model_content == {
        "role": "model",
        "parts": [{
            "functionCall": {"name": "read_inventory", "args": {}, "id": "call-17"},
            "thoughtSignature": "b3BhcXVlLXNpZ25hdHVyZQ==",
        }],
    }


class ScriptedProvider:
    def __init__(self, scripts):
        self.scripts = {model: list(items) for model, items in scripts.items()}
        self.calls = []

    def generate(self, model, contents, functions):
        self.calls.append((model, contents, functions))
        result = self.scripts[model].pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_loads_config_from_env_file_without_exposing_key(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_PRIMARY_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_FALLBACK_MODELS", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GEMINI_API_KEY=test-secret\nGEMINI_PRIMARY_MODEL=model-a\nGEMINI_FALLBACK_MODELS=model-b, model-a\n",
        encoding="utf-8",
    )
    config = load_gemini_config(env_file)

    assert config.models == ("model-a", "model-b")
    assert "test-secret" not in repr(config)


def test_missing_credentials_or_unverified_model_names_fail_clearly(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_PRIMARY_MODEL", raising=False)
    with pytest.raises(ModelConfigurationError, match="GEMINI_API_KEY"):
        load_gemini_config(tmp_path / "missing.env")

    (tmp_path / ".env").write_text(
        "GEMINI_API_KEY=secret\nGEMINI_PRIMARY_MODEL=your_primary_model\n", encoding="utf-8"
    )
    with pytest.raises(ModelConfigurationError, match="GEMINI_PRIMARY_MODEL"):
        load_gemini_config(tmp_path / ".env")


def test_incompatible_model_falls_back_and_logs_each_request(tmp_path):
    incompatible = RuntimeError("function calling is not supported")
    provider = ScriptedProvider({
        "primary": [incompatible],
        "fallback": [response(calls=[FunctionCall("read_file", {"path": "policy.md"})])],
    })
    log = EventLog(tmp_path / "events.jsonl")
    client = GeminiClient(GeminiConfig("secret", ("primary", "fallback")), provider, event_log=log)

    reply = client.generate([], [{"name": "read_file"}], run_id="run-1", step=1)

    assert reply.model == "fallback"
    assert reply.function_call == FunctionCall("read_file", {"path": "policy.md"})
    events = log.read_recent()
    assert [(event["model"], event["outcome"]) for event in events] == [
        ("fallback", "ok"), ("primary", "incompatible")
    ]
    assert events[1]["error_type"] == "RuntimeError"
    assert events[1]["status_code"] is None
    assert "secret" not in (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert "function calling is not supported" not in (tmp_path / "events.jsonl").read_text(encoding="utf-8")


def test_transient_failure_retries_twice_then_uses_fallback():
    transient = RuntimeError("temporary server error")
    transient.code = 503
    provider = ScriptedProvider({
        "primary": [transient, transient, transient],
        "fallback": [response(text="done")],
    })
    sleeps = []
    client = GeminiClient(GeminiConfig("secret", ("primary", "fallback")), provider,
                          sleep=sleeps.append)

    reply = client.generate([], [], run_id="run-2", step=1)

    assert reply.text == "done"
    assert [call[0] for call in provider.calls] == ["primary"] * 3 + ["fallback"]
    assert sleeps == [0.25, 0.5]


def test_malformed_response_gets_one_nudge_then_fails_the_step():
    provider = ScriptedProvider({"primary": [response(), response()]})
    client = GeminiClient(GeminiConfig("secret", ("primary",)), provider, sleep=lambda _: None)

    with pytest.raises(ModelResponseError, match="empty response"):
        client.generate([{"role": "user", "parts": [{"text": "do work"}]}], [],
                        run_id="run-3", step=1)
    assert len(provider.calls) == 2
    assert "Return one valid function call" in provider.calls[1][1][-1]["parts"][0]["text"]


def test_auth_error_stops_without_trying_fallback():
    auth = RuntimeError("invalid api key")
    auth.code = 403
    provider = ScriptedProvider({"primary": [auth], "fallback": [response(text="must not run")]})
    client = GeminiClient(GeminiConfig("secret", ("primary", "fallback")), provider)

    with pytest.raises(ModelConfigurationError, match="authentication failed"):
        client.generate([], [], run_id="run-4", step=1)
    assert [call[0] for call in provider.calls] == ["primary"]


def test_all_unavailable_models_are_not_retried_on_later_turns():
    provider = ScriptedProvider({"primary": [RuntimeError("provider offline")]})
    client = GeminiClient(GeminiConfig("secret", ("primary",)), provider)

    with pytest.raises(ModelUnavailableError):
        client.generate([], [], run_id="run-5", step=1)
    with pytest.raises(ModelUnavailableError):
        client.generate([], [], run_id="run-5", step=2)
    assert len(provider.calls) == 1
