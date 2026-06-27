"""Unit tests for request payload validation (_validate is a pure function)."""
from __future__ import annotations

from app.routes.chat import _validate


def _base() -> dict:
    return {"model": "mock/echo", "messages": [{"role": "user", "content": "hi"}]}


# ── Valid payloads ────────────────────────────────────────────────────────────

def test_minimal_valid_payload():
    assert _validate(_base()) is None


def test_valid_with_all_optional_fields():
    payload = {
        "model": "mock/echo",
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ],
        "temperature": 0.7,
        "top_p": 0.9,
        "max_tokens": 256,
        "stop": ["\n"],
        "user": "user-abc",
        "metadata": {"source": "unit-test"},
    }
    assert _validate(payload) is None


def test_temperature_boundary_zero():
    p = {**_base(), "temperature": 0}
    assert _validate(p) is None


def test_temperature_boundary_two():
    p = {**_base(), "temperature": 2}
    assert _validate(p) is None


# ── Missing / empty required fields ──────────────────────────────────────────

def test_missing_model():
    err = _validate({"messages": [{"role": "user", "content": "hi"}]})
    assert err is not None
    assert "model" in err


def test_empty_model():
    err = _validate({"model": "", "messages": [{"role": "user", "content": "hi"}]})
    assert err is not None


def test_missing_messages():
    err = _validate({"model": "mock/echo"})
    assert err is not None
    assert "messages" in err


def test_empty_messages_list():
    err = _validate({"model": "mock/echo", "messages": []})
    assert err is not None


def test_messages_not_a_list():
    err = _validate({"model": "mock/echo", "messages": "bad"})
    assert err is not None


# ── Message-level validation ──────────────────────────────────────────────────

def test_invalid_role():
    err = _validate({"model": "mock/echo", "messages": [{"role": "robot", "content": "hi"}]})
    assert err is not None
    assert "role" in err


def test_message_not_object():
    err = _validate({"model": "mock/echo", "messages": ["plain string"]})
    assert err is not None


# ── Parameter range validation ────────────────────────────────────────────────

def test_temperature_too_high():
    err = _validate({**_base(), "temperature": 2.1})
    assert err is not None
    assert "temperature" in err


def test_temperature_negative():
    err = _validate({**_base(), "temperature": -0.1})
    assert err is not None


def test_top_p_out_of_range():
    err = _validate({**_base(), "top_p": 1.1})
    assert err is not None
    assert "top_p" in err


def test_max_tokens_zero():
    err = _validate({**_base(), "max_tokens": 0})
    assert err is not None
    assert "max_tokens" in err


def test_max_tokens_negative():
    err = _validate({**_base(), "max_tokens": -10})
    assert err is not None


def test_max_tokens_float_rejected():
    err = _validate({**_base(), "max_tokens": 10.5})
    assert err is not None
