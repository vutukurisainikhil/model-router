"""Unit tests for ModelRegistry."""
from __future__ import annotations

from app.router.model_registry import build_registry, ModelRegistry


def _registry(default: str = "anthropic-claude-4.6-sonnet") -> ModelRegistry:
    return ModelRegistry(build_registry(default))


def test_resolve_default_do_model():
    reg = _registry("anthropic-claude-4.6-sonnet")
    entry = reg.resolve("do/anthropic-claude-4.6-sonnet")
    assert entry is not None
    assert entry["primary"]["provider"] == "do"
    assert entry["primary"]["model"] == "anthropic-claude-4.6-sonnet"


def test_resolve_llama_model():
    entry = _registry().resolve("do/llama3.3-70b-instruct")
    assert entry is not None
    assert entry["primary"]["model"] == "llama3.3-70b-instruct"


def test_resolve_mock_model():
    entry = _registry().resolve("mock/echo")
    assert entry is not None
    assert entry["primary"]["provider"] == "mock"


def test_resolve_unknown_returns_none():
    assert _registry().resolve("unknown/model-xyz") is None


def test_known_models_includes_mock():
    assert "mock/echo" in _registry().known_models()


def test_known_models_includes_default():
    assert "do/anthropic-claude-4.6-sonnet" in _registry().known_models()


def test_build_registry_custom_default():
    reg = ModelRegistry(build_registry("my-custom-model"))
    assert reg.resolve("do/my-custom-model") is not None
    assert reg.resolve("do/my-custom-model")["primary"]["model"] == "my-custom-model"
