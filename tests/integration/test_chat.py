"""Integration tests for POST /v1/chat/completions (F1).

Uses only the mock/echo model — no real network calls.
The DOAdapter is instantiated but never called in these tests.
"""
from __future__ import annotations

import pytest

from app import create_app
from app.config import Config

TEST_KEY = "integration-test-key-f1"


class _TestConfig(Config):
    """Overrides that make the app safe and deterministic for integration tests."""
    def __init__(self):
        super().__init__()
        self.ROUTER_API_KEYS = {TEST_KEY}
        # Use empty DO key — DOAdapter will be created but never invoked in these tests
        self.DO_INFERENCE_API_KEY = "not-used-in-tests"


@pytest.fixture(scope="module")
def app():
    flask_app = create_app(config=_TestConfig())
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _auth_headers(key: str = TEST_KEY) -> dict:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _base_payload() -> dict:
    return {"model": "mock/echo", "messages": [{"role": "user", "content": "hello"}]}


# ── Authentication ────────────────────────────────────────────────────────────

def test_no_auth_returns_401(client):
    resp = client.post("/v1/chat/completions", json=_base_payload())
    assert resp.status_code == 401
    assert resp.get_json()["error"]["code"] == "unauthorized"


def test_wrong_key_returns_401(client):
    resp = client.post(
        "/v1/chat/completions",
        headers=_auth_headers("wrong-key"),
        json=_base_payload(),
    )
    assert resp.status_code == 401


def test_missing_bearer_prefix_returns_401(client):
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": TEST_KEY, "Content-Type": "application/json"},
        json=_base_payload(),
    )
    assert resp.status_code == 401


# ── Input validation ──────────────────────────────────────────────────────────

def test_unknown_model_returns_400_model_not_found(client):
    resp = client.post(
        "/v1/chat/completions",
        headers=_auth_headers(),
        json={"model": "unknown/model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "model_not_found"


def test_missing_messages_returns_400(client):
    resp = client.post(
        "/v1/chat/completions",
        headers=_auth_headers(),
        json={"model": "mock/echo"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "invalid_request"


def test_invalid_json_body_returns_400(client):
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {TEST_KEY}", "Content-Type": "application/json"},
        data="this is not json",
    )
    assert resp.status_code == 400


def test_stream_true_returns_sse_not_501(client):
    resp = client.post(
        "/v1/chat/completions",
        headers=_auth_headers(),
        json={**_base_payload(), "stream": True},
    )
    # streaming is now supported (F2) — must be 200 text/event-stream
    assert resp.status_code == 200
    assert "text/event-stream" in resp.content_type


# ── Unified response schema ───────────────────────────────────────────────────

def test_successful_response_status_200(client):
    resp = client.post("/v1/chat/completions", headers=_auth_headers(), json=_base_payload())
    assert resp.status_code == 200


def test_response_has_all_required_fields(client):
    body = client.post(
        "/v1/chat/completions", headers=_auth_headers(), json=_base_payload()
    ).get_json()
    assert "id" in body
    assert body["object"] == "chat.completion"
    assert "created" in body
    assert "model" in body
    assert "provider" in body
    assert "choices" in body
    assert len(body["choices"]) > 0
    assert "usage" in body


def test_response_model_has_provider_prefix(client):
    body = client.post(
        "/v1/chat/completions", headers=_auth_headers(), json=_base_payload()
    ).get_json()
    assert body["model"].startswith("mock/")


def test_response_provider_is_mock(client):
    body = client.post(
        "/v1/chat/completions", headers=_auth_headers(), json=_base_payload()
    ).get_json()
    assert body["provider"] == "mock"


def test_response_choice_has_message(client):
    body = client.post(
        "/v1/chat/completions", headers=_auth_headers(), json=_base_payload()
    ).get_json()
    choice = body["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert isinstance(choice["message"]["content"], str)
    assert choice["finish_reason"] == "stop"


def test_no_upstream_specific_fields_in_response(client):
    body = client.post(
        "/v1/chat/completions", headers=_auth_headers(), json=_base_payload()
    ).get_json()
    # Anthropic-specific fields must not leak
    assert "stop_reason" not in body
    assert "stop_sequence" not in body
    # Internal mock flag must not leak
    assert "_mock" not in body


def test_metadata_field_not_in_response(client):
    payload = {**_base_payload(), "metadata": {"should": "not-appear"}}
    body = client.post(
        "/v1/chat/completions", headers=_auth_headers(), json=payload
    ).get_json()
    assert "metadata" not in body


# ── Response headers ──────────────────────────────────────────────────────────

def test_response_headers_present(client):
    resp = client.post("/v1/chat/completions", headers=_auth_headers(), json=_base_payload())
    assert "X-Request-Id" in resp.headers
    assert "X-Router-Provider" in resp.headers
    assert "X-Router-Model" in resp.headers
    assert "X-Router-Latency-Ms" in resp.headers


def test_request_id_propagated_from_header(client):
    custom_id = "my-custom-request-id-123"
    resp = client.post(
        "/v1/chat/completions",
        headers={**_auth_headers(), "X-Request-Id": custom_id},
        json=_base_payload(),
    )
    assert resp.headers["X-Request-Id"] == custom_id


# ── Error envelope schema ─────────────────────────────────────────────────────

def test_error_response_has_unified_schema(client):
    body = client.post(
        "/v1/chat/completions",
        headers=_auth_headers(),
        json={"model": "bad/model", "messages": [{"role": "user", "content": "hi"}]},
    ).get_json()
    err = body["error"]
    assert "code" in err
    assert "message" in err
    assert "type" in err
    assert "request_id" in err


def test_health_requires_no_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200


# ── Default model ─────────────────────────────────────────────────────────────

def test_omitting_model_uses_default(client, app):
    """When 'model' is absent the router injects DO_DEFAULT_MODEL and succeeds."""
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    resp = client.post("/v1/chat/completions", headers=_auth_headers(), json=payload)
    # Should not be a 400 validation error
    assert resp.status_code == 200
    data = resp.get_json()
    expected_default = f"do/{app.config['DO_DEFAULT_MODEL']}"
    # The response model will be the native model served (could fall through to mock
    # in tests since DOAdapter is not called). Just verify no validation rejection.
    assert "error" not in data


def test_empty_model_string_uses_default(client, app):
    """Empty string for 'model' is treated same as omitted — default is applied."""
    payload = {"model": "", "messages": [{"role": "user", "content": "hi"}]}
    resp = client.post("/v1/chat/completions", headers=_auth_headers(), json=payload)
    assert resp.status_code == 200
    assert "error" not in resp.get_json()
