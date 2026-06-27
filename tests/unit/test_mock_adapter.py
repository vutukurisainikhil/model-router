"""Unit tests for MockAdapter."""
from __future__ import annotations

from app.adapters.mock import MockAdapter


def _adapter() -> MockAdapter:
    return MockAdapter()


def test_call_returns_dict():
    result = _adapter().call(
        {"model": "mock/echo", "messages": [{"role": "user", "content": "Hello world"}]},
        stream=False,
    )
    assert isinstance(result, dict)


def test_call_echoes_user_content():
    result = _adapter().call(
        {"model": "mock/echo", "messages": [{"role": "user", "content": "ping"}]},
        stream=False,
    )
    assert "ping" in result["choices"][0]["message"]["content"]


def test_call_has_required_fields():
    result = _adapter().call(
        {"model": "mock/echo", "messages": [{"role": "user", "content": "hi"}]},
        stream=False,
    )
    assert "id" in result
    assert result["object"] == "chat.completion"
    assert "created" in result
    assert "usage" in result


def test_translate_response_adds_provider():
    adapter = _adapter()
    native = adapter.call(
        {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}, stream=False
    )
    unified = adapter.translate_response(native)
    assert unified["provider"] == "mock"


def test_translate_response_prefixes_model():
    adapter = _adapter()
    native = adapter.call(
        {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}, stream=False
    )
    unified = adapter.translate_response(native)
    assert unified["model"].startswith("mock/")


def test_translate_response_removes_internal_flag():
    adapter = _adapter()
    native = adapter.call(
        {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}, stream=False
    )
    assert "_mock" in native  # present before translate
    unified = adapter.translate_response(native)
    assert "_mock" not in unified  # stripped after translate
