"""Unit tests for DOAdapter — no network calls."""
from __future__ import annotations

from unittest.mock import MagicMock

from app.adapters.do import DOAdapter


def _adapter() -> DOAdapter:
    return DOAdapter("https://inference.do-ai.run/v1", "test-key")


# ── translate_request ─────────────────────────────────────────────────────────

def test_translate_request_strips_do_prefix():
    native = _adapter().translate_request(
        {"model": "do/anthropic-claude-4.6-sonnet", "messages": []}
    )
    assert native["model"] == "anthropic-claude-4.6-sonnet"


def test_translate_request_no_prefix_unchanged():
    native = _adapter().translate_request(
        {"model": "llama3.3-70b-instruct", "messages": []}
    )
    assert native["model"] == "llama3.3-70b-instruct"


def test_translate_request_removes_metadata():
    native = _adapter().translate_request(
        {
            "model": "do/llama3.3-70b-instruct",
            "messages": [],
            "metadata": {"source": "test"},
        }
    )
    assert "metadata" not in native


def test_translate_request_preserves_messages_and_params():
    messages = [{"role": "user", "content": "Hello"}]
    native = _adapter().translate_request(
        {
            "model": "do/llama3.3-70b-instruct",
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 100,
            "top_p": 0.9,
        }
    )
    assert native["messages"] == messages
    assert native["temperature"] == 0.7
    assert native["max_tokens"] == 100
    assert native["top_p"] == 0.9


def test_translate_request_does_not_mutate_input():
    original = {"model": "do/llama3.3-70b-instruct", "messages": [], "metadata": {}}
    _adapter().translate_request(original)
    assert "metadata" in original  # original untouched


# ── translate_response ────────────────────────────────────────────────────────

def _mock_http_response(model: str = "anthropic-claude-4.6-sonnet") -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {
        "id": "chatcmpl-abc123",
        "object": "chat.completion",
        "created": 1234567890,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello there!"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }
    return resp


def test_translate_response_adds_provider():
    result = _adapter().translate_response(_mock_http_response())
    assert result["provider"] == "do"


def test_translate_response_prefixes_model():
    result = _adapter().translate_response(_mock_http_response("anthropic-claude-4.6-sonnet"))
    assert result["model"] == "do/anthropic-claude-4.6-sonnet"


def test_translate_response_no_double_prefix():
    result = _adapter().translate_response(_mock_http_response("do/already-prefixed"))
    assert result["model"] == "do/already-prefixed"


def test_translate_response_preserves_choices():
    result = _adapter().translate_response(_mock_http_response())
    assert result["choices"][0]["message"]["content"] == "Hello there!"
    assert result["choices"][0]["finish_reason"] == "stop"


def test_translate_response_preserves_usage():
    result = _adapter().translate_response(_mock_http_response())
    assert result["usage"]["total_tokens"] == 8
