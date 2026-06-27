"""Integration tests for F3: Resilient Fallback Routing.

Uses patched DO adapter to simulate failures — no real network calls.
Each test gets a fresh app (function scope) for circuit breaker isolation.
"""
from __future__ import annotations

import json

import httpx
import pytest
from unittest.mock import MagicMock, patch

from app import create_app
from app.config import Config
from app.router.circuit_breaker import CircuitBreaker

TEST_KEY = "test-fallback-key-f3"


class _TestConfig(Config):
    def __init__(self):
        super().__init__()
        self.ROUTER_API_KEYS = {TEST_KEY}
        self.DO_INFERENCE_API_KEY = "not-used-in-tests"


@pytest.fixture
def app():
    """Function-scoped app so circuit breaker state never leaks between tests."""
    flask_app = create_app(config=_TestConfig())
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def do_adapter(app):
    return app.extensions["orchestrator"]._adapters["do"]


@pytest.fixture
def mock_adapter(app):
    return app.extensions["orchestrator"]._adapters["mock"]


@pytest.fixture
def orchestrator(app):
    return app.extensions["orchestrator"]


def _auth():
    return {"Authorization": f"Bearer {TEST_KEY}", "Content-Type": "application/json"}


def _base():
    return {"model": "do/llama3.3-70b-instruct", "messages": [{"role": "user", "content": "hi"}]}


def _make_http_error(status: int) -> httpx.HTTPStatusError:
    mock_resp = MagicMock()
    mock_resp.status_code = status
    return httpx.HTTPStatusError(f"HTTP {status}", request=MagicMock(), response=mock_resp)


def _parse_stream_chunks(raw: bytes) -> list[dict]:
    chunks = []
    for block in raw.decode().split("\n\n"):
        block = block.strip()
        if not block or block == "data: [DONE]":
            continue
        if block.startswith("data: "):
            try:
                chunks.append(json.loads(block[6:]))
            except json.JSONDecodeError:
                pass
    return chunks


# ── Non-streaming fallback ────────────────────────────────────────────────────

def test_fallback_on_503_returns_200_from_mock(client, do_adapter):
    with patch.object(do_adapter, "call", side_effect=_make_http_error(503)):
        resp = client.post("/v1/chat/completions", headers=_auth(), json=_base())
    assert resp.status_code == 200
    assert resp.get_json()["provider"] == "mock"


def test_fallback_on_503_attempt_header_is_2(client, do_adapter):
    with patch.object(do_adapter, "call", side_effect=_make_http_error(503)):
        resp = client.post("/v1/chat/completions", headers=_auth(), json=_base())
    assert int(resp.headers["X-Router-Attempts"]) == 2


def test_fallback_on_502_returns_200(client, do_adapter):
    with patch.object(do_adapter, "call", side_effect=_make_http_error(502)):
        resp = client.post("/v1/chat/completions", headers=_auth(), json=_base())
    assert resp.status_code == 200


def test_fallback_on_timeout_returns_200(client, do_adapter):
    with patch.object(do_adapter, "call", side_effect=httpx.ReadTimeout("timed out")):
        resp = client.post("/v1/chat/completions", headers=_auth(), json=_base())
    assert resp.status_code == 200
    assert resp.get_json()["provider"] == "mock"


def test_fallback_on_429_returns_200(client, do_adapter):
    with patch.object(do_adapter, "call", side_effect=_make_http_error(429)):
        resp = client.post("/v1/chat/completions", headers=_auth(), json=_base())
    assert resp.status_code == 200


# ── Non-retryable errors ──────────────────────────────────────────────────────

def test_non_retryable_401_not_forwarded_to_mock(client, do_adapter, mock_adapter):
    mock_call_count = []

    def counting_mock_call(native, *, stream):
        mock_call_count.append(1)
        return MagicMock()

    with patch.object(do_adapter, "call", side_effect=_make_http_error(401)), \
         patch.object(mock_adapter, "call", new=counting_mock_call):
        resp = client.post("/v1/chat/completions", headers=_auth(), json=_base())

    # Mock should NOT be called — 401 is non-retryable
    assert len(mock_call_count) == 0
    assert resp.status_code != 200


def test_non_retryable_403_not_retried(client, do_adapter, mock_adapter):
    mock_call_count = []

    def counting_mock_call(native, *, stream):
        mock_call_count.append(1)
        return MagicMock()

    with patch.object(do_adapter, "call", side_effect=_make_http_error(403)), \
         patch.object(mock_adapter, "call", new=counting_mock_call):
        resp = client.post("/v1/chat/completions", headers=_auth(), json=_base())

    assert len(mock_call_count) == 0


# ── Exhausted chain ───────────────────────────────────────────────────────────

def test_exhausted_chain_returns_502(client, do_adapter, mock_adapter):
    with patch.object(do_adapter, "call", side_effect=_make_http_error(503)), \
         patch.object(mock_adapter, "call", side_effect=_make_http_error(503)):
        resp = client.post("/v1/chat/completions", headers=_auth(), json=_base())
    assert resp.status_code == 502
    assert resp.get_json()["error"]["code"] == "upstream_error"


def test_all_timeouts_returns_408(client, do_adapter, mock_adapter):
    with patch.object(do_adapter, "call", side_effect=httpx.ReadTimeout("t/o")), \
         patch.object(mock_adapter, "call", side_effect=httpx.ReadTimeout("t/o")):
        resp = client.post("/v1/chat/completions", headers=_auth(), json=_base())
    assert resp.status_code == 408
    assert resp.get_json()["error"]["code"] == "upstream_timeout"


def test_all_429s_returns_429(client, do_adapter, mock_adapter):
    with patch.object(do_adapter, "call", side_effect=_make_http_error(429)), \
         patch.object(mock_adapter, "call", side_effect=_make_http_error(429)):
        resp = client.post("/v1/chat/completions", headers=_auth(), json=_base())
    assert resp.status_code == 429
    assert resp.get_json()["error"]["code"] == "rate_limited"


# ── Circuit breaker ───────────────────────────────────────────────────────────

def test_circuit_breaker_opens_and_skips_do(client, orchestrator, mock_adapter):
    """After enough failures, breaker opens → DO skipped → mock serves request."""
    breaker = orchestrator._breaker
    for _ in range(10):  # min_samples=10, all failures → 100% > 50%
        breaker.record_failure("do", "llama3.3-70b-instruct")

    assert breaker.get_state("do", "llama3.3-70b-instruct") == CircuitBreaker.OPEN
    # DO's call should NOT be triggered (breaker open)
    resp = client.post("/v1/chat/completions", headers=_auth(), json=_base())
    assert resp.status_code == 200
    assert resp.get_json()["provider"] == "mock"


def test_circuit_breaker_closed_on_fresh_app(orchestrator):
    """Fresh app has all breakers closed."""
    assert orchestrator._breaker.get_state("do", "llama3.3-70b-instruct") == CircuitBreaker.CLOSED


def test_circuit_breaker_closes_after_recovery(orchestrator):
    """Breaker closes when a success comes in during HALF_OPEN."""
    t = [0.0]
    breaker = CircuitBreaker(min_samples=5, cooldown_s=30.0, clock=lambda: t[0])
    orchestrator._breaker = breaker

    for _ in range(5):
        breaker.record_failure("do", "llama3.3-70b-instruct")
    t[0] = 31.0
    breaker.is_open("do", "llama3.3-70b-instruct")  # → HALF_OPEN
    breaker.record_success("do", "llama3.3-70b-instruct")
    assert breaker.get_state("do", "llama3.3-70b-instruct") == CircuitBreaker.CLOSED


# ── Streaming fallback ────────────────────────────────────────────────────────

def test_stream_fallback_pre_first_byte(client, do_adapter):
    """DO fails before first chunk → fallback to mock → client sees clean stream."""
    with patch.object(do_adapter, "call", side_effect=_make_http_error(503)):
        resp = client.post("/v1/chat/completions", headers=_auth(),
                           json={**_base(), "stream": True})

    assert resp.status_code == 200
    assert "text/event-stream" in resp.content_type
    raw = resp.data.decode()
    assert "data: [DONE]" in raw

    chunks = _parse_stream_chunks(resp.data)
    assert len(chunks) > 0
    # All chunks should be from mock (fallback)
    assert all(c.get("provider") == "mock" for c in chunks)
    # No error chunks
    finish_reasons = [c["choices"][0].get("finish_reason") for c in chunks]
    assert "error" not in finish_reasons


def test_stream_mid_stream_abort_yields_error_chunk(client, do_adapter):
    """DO streams one chunk then dies → error chunk + [DONE], NO fallback to mock."""
    mock_chunks_served = []

    def _one_chunk_then_die(native, *, stream):
        if not stream:
            raise _make_http_error(503)

        def _gen():
            yield (
                'data: {"id":"x","object":"chat.completion.chunk",'
                '"model":"llama3.3-70b-instruct",'
                '"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}'
            )
            raise httpx.ReadTimeout("mid-stream timeout")

        return _gen()

    def _mock_call_tracker(native, *, stream):
        mock_chunks_served.append(1)
        return MagicMock()

    from app.adapters.mock import MockAdapter
    real_mock = client.application.extensions["orchestrator"]._adapters["mock"]

    with patch.object(do_adapter, "call", new=_one_chunk_then_die), \
         patch.object(real_mock, "call", new=_mock_call_tracker):
        resp = client.post("/v1/chat/completions", headers=_auth(),
                           json={**_base(), "stream": True})

    raw = resp.data.decode()
    assert "data: [DONE]" in raw

    chunks = _parse_stream_chunks(resp.data)
    # Last chunk must have finish_reason=error
    assert chunks[-1]["choices"][0].get("finish_reason") == "error"
    # Mock must NOT have been called (mid-stream rule: no fallback)
    assert len(mock_chunks_served) == 0


def test_stream_fallback_exhausted_yields_error_chunk(client, do_adapter, mock_adapter):
    """All targets fail before first chunk → error chunk + [DONE]."""
    with patch.object(do_adapter, "call", side_effect=_make_http_error(503)), \
         patch.object(mock_adapter, "call", side_effect=_make_http_error(503)):
        resp = client.post("/v1/chat/completions", headers=_auth(),
                           json={**_base(), "stream": True})

    assert resp.status_code == 200
    raw = resp.data.decode()
    assert "data: [DONE]" in raw
    chunks = _parse_stream_chunks(resp.data)
    assert chunks[-1]["choices"][0].get("finish_reason") == "error"
