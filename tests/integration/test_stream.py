"""Integration tests for SSE streaming (F2).

All tests use mock/echo — no real network calls.
"""
from __future__ import annotations

import json

import pytest

from app import create_app
from app.config import Config

TEST_KEY = "integration-test-key-f2"


class _TestConfig(Config):
    def __init__(self):
        super().__init__()
        self.ROUTER_API_KEYS = {TEST_KEY}
        self.DO_INFERENCE_API_KEY = "not-used-in-tests"


@pytest.fixture(scope="module")
def app():
    flask_app = create_app(config=_TestConfig())
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _auth() -> dict:
    return {"Authorization": f"Bearer {TEST_KEY}", "Content-Type": "application/json"}


def _stream_payload(content: str = "hello") -> dict:
    return {"model": "mock/echo", "stream": True, "messages": [{"role": "user", "content": content}]}


def _parse_events(raw: bytes) -> list[dict]:
    """Parse all non-DONE SSE data events into a list of chunk dicts."""
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


# ── Content-type and status ───────────────────────────────────────────────────

def test_stream_returns_200(client):
    resp = client.post("/v1/chat/completions", headers=_auth(), json=_stream_payload())
    assert resp.status_code == 200


def test_stream_content_type_is_event_stream(client):
    resp = client.post("/v1/chat/completions", headers=_auth(), json=_stream_payload())
    assert "text/event-stream" in resp.content_type


# ── SSE wire format ───────────────────────────────────────────────────────────

def test_stream_ends_with_done_sentinel(client):
    resp = client.post("/v1/chat/completions", headers=_auth(), json=_stream_payload())
    raw = resp.data.decode()
    assert "data: [DONE]" in raw


def test_stream_events_start_with_data_prefix(client):
    resp = client.post("/v1/chat/completions", headers=_auth(), json=_stream_payload())
    lines = [l for l in resp.data.decode().split("\n") if l.strip()]
    data_lines = [l for l in lines if l.startswith("data: ")]
    assert len(data_lines) > 0


def test_stream_events_separated_by_double_newline(client):
    resp = client.post("/v1/chat/completions", headers=_auth(), json=_stream_payload())
    raw = resp.data.decode()
    # Each frame ends with \n\n  (at least 2 frames: first chunk + [DONE])
    assert raw.count("\n\n") >= 2


# ── Chunk schema ──────────────────────────────────────────────────────────────

def test_stream_chunks_have_correct_object_type(client):
    chunks = _parse_events(
        client.post("/v1/chat/completions", headers=_auth(), json=_stream_payload()).data
    )
    for c in chunks:
        assert c.get("object") == "chat.completion.chunk"


def test_stream_chunks_have_id_field(client):
    chunks = _parse_events(
        client.post("/v1/chat/completions", headers=_auth(), json=_stream_payload()).data
    )
    for c in chunks:
        assert "id" in c


def test_stream_chunks_have_choices(client):
    chunks = _parse_events(
        client.post("/v1/chat/completions", headers=_auth(), json=_stream_payload()).data
    )
    for c in chunks:
        assert "choices" in c
        assert len(c["choices"]) > 0


# ── Ordering invariants (spec §F2.2) ─────────────────────────────────────────

def test_stream_first_chunk_has_role(client):
    chunks = _parse_events(
        client.post("/v1/chat/completions", headers=_auth(), json=_stream_payload()).data
    )
    assert chunks[0]["choices"][0]["delta"].get("role") == "assistant"


def test_stream_last_chunk_has_finish_reason_stop(client):
    chunks = _parse_events(
        client.post("/v1/chat/completions", headers=_auth(), json=_stream_payload()).data
    )
    last = chunks[-1]
    assert last["choices"][0].get("finish_reason") == "stop"


def test_stream_middle_chunks_have_content(client):
    chunks = _parse_events(
        client.post("/v1/chat/completions", headers=_auth(), json=_stream_payload("ping pong boom")).data
    )
    # There should be at least one content chunk between first (role) and last (finish)
    middle = chunks[1:-1]
    has_content = any(c["choices"][0]["delta"].get("content") for c in middle)
    assert has_content


def test_stream_unified_model_prefix(client):
    chunks = _parse_events(
        client.post("/v1/chat/completions", headers=_auth(), json=_stream_payload()).data
    )
    for c in chunks:
        assert c.get("model", "").startswith("mock/")


def test_stream_provider_is_mock(client):
    chunks = _parse_events(
        client.post("/v1/chat/completions", headers=_auth(), json=_stream_payload()).data
    )
    for c in chunks:
        assert c.get("provider") == "mock"


# ── Pre-stream error handling ─────────────────────────────────────────────────

def test_stream_unknown_model_returns_400_not_sse(client):
    resp = client.post(
        "/v1/chat/completions",
        headers=_auth(),
        json={"model": "unknown/xyz", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )
    # Must be a proper HTTP error, not an SSE stream
    assert resp.status_code == 400
    assert resp.content_type == "application/json"
    assert resp.get_json()["error"]["code"] == "model_not_found"


def test_stream_missing_auth_returns_401(client):
    resp = client.post("/v1/chat/completions", json=_stream_payload())
    assert resp.status_code == 401
    assert resp.content_type == "application/json"


def test_stream_invalid_messages_returns_400(client):
    resp = client.post(
        "/v1/chat/completions",
        headers=_auth(),
        json={"model": "mock/echo", "stream": True, "messages": []},
    )
    assert resp.status_code == 400


# ── Response headers ──────────────────────────────────────────────────────────

def test_stream_has_request_id_header(client):
    resp = client.post("/v1/chat/completions", headers=_auth(), json=_stream_payload())
    assert "X-Request-Id" in resp.headers


def test_stream_has_provider_header(client):
    resp = client.post("/v1/chat/completions", headers=_auth(), json=_stream_payload())
    assert resp.headers.get("X-Router-Provider") == "mock"


def test_stream_has_model_header(client):
    resp = client.post("/v1/chat/completions", headers=_auth(), json=_stream_payload())
    assert "X-Router-Model" in resp.headers


def test_stream_request_id_propagated_from_header(client):
    custom_id = "stream-request-id-abc"
    resp = client.post(
        "/v1/chat/completions",
        headers={**_auth(), "X-Request-Id": custom_id},
        json=_stream_payload(),
    )
    assert resp.headers.get("X-Request-Id") == custom_id
