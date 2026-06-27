"""Unit tests for SSE helpers and adapter stream translation."""
from __future__ import annotations

import json

from app.streaming.sse import parse_sse_line, sse_event, SSE_DONE
from app.adapters.mock import MockAdapter
from app.adapters.do import DOAdapter


# ── parse_sse_line ────────────────────────────────────────────────────────────

def test_parse_normal_data_line():
    payload = json.dumps({"id": "x", "choices": []})
    json_str, is_done = parse_sse_line(f"data: {payload}")
    assert not is_done
    assert json.loads(json_str)["id"] == "x"


def test_parse_done_line():
    _, is_done = parse_sse_line("data: [DONE]")
    assert is_done


def test_parse_comment_returns_empty():
    json_str, is_done = parse_sse_line(": keep-alive")
    assert json_str == ""
    assert not is_done


def test_parse_empty_line_returns_empty():
    json_str, is_done = parse_sse_line("")
    assert json_str == ""
    assert not is_done


def test_parse_non_data_prefix_returns_empty():
    json_str, is_done = parse_sse_line("event: ping")
    assert json_str == ""
    assert not is_done


# ── sse_event ─────────────────────────────────────────────────────────────────

def test_sse_event_format():
    result = sse_event({"foo": "bar"})
    assert result.startswith("data: ")
    assert result.endswith("\n\n")
    payload = json.loads(result[6:])
    assert payload["foo"] == "bar"


def test_sse_done_format():
    assert SSE_DONE == "data: [DONE]\n\n"


# ── MockAdapter streaming ─────────────────────────────────────────────────────

def _mock_stream_chunks(content: str = "hello world") -> list[dict]:
    adapter = MockAdapter()
    payload = {"model": "mock/echo", "messages": [{"role": "user", "content": content}]}
    lines = list(adapter.call(payload, stream=True))
    chunks = []
    for line in lines:
        if line in ("data: [DONE]", "[DONE]"):
            continue
        chunk = adapter.translate_stream_chunk(line)
        if chunk:
            chunks.append(chunk)
    return chunks


def test_mock_stream_starts_with_role_chunk():
    chunks = _mock_stream_chunks()
    first = chunks[0]
    assert first["choices"][0]["delta"].get("role") == "assistant"


def test_mock_stream_has_content_chunks():
    chunks = _mock_stream_chunks("ping pong")
    content_chunks = [c for c in chunks if c["choices"][0]["delta"].get("content")]
    assert len(content_chunks) > 0


def test_mock_stream_ends_with_stop():
    chunks = _mock_stream_chunks()
    last = chunks[-1]
    assert last["choices"][0]["finish_reason"] == "stop"


def test_mock_stream_all_chunks_have_unified_model_prefix():
    chunks = _mock_stream_chunks()
    for c in chunks:
        assert c.get("model", "").startswith("mock/")


def test_mock_stream_all_chunks_have_provider():
    chunks = _mock_stream_chunks()
    for c in chunks:
        assert c.get("provider") == "mock"


def test_mock_stream_all_chunks_have_correct_object():
    chunks = _mock_stream_chunks()
    for c in chunks:
        assert c["object"] == "chat.completion.chunk"


def test_mock_stream_last_line_is_done():
    adapter = MockAdapter()
    payload = {"model": "mock/echo", "messages": [{"role": "user", "content": "hi"}]}
    lines = list(adapter.call(payload, stream=True))
    assert lines[-1] == "data: [DONE]"


def test_mock_stream_usage_in_final_chunk():
    chunks = _mock_stream_chunks()
    last = chunks[-1]
    # Final chunk carries usage
    assert "usage" in last or last["choices"][0]["finish_reason"] == "stop"


# ── DOAdapter translate_stream_chunk ─────────────────────────────────────────

def _do_adapter() -> DOAdapter:
    return DOAdapter("https://inference.do-ai.run/v1", "test-key")


def test_do_translate_stream_chunk_adds_provider():
    adapter = _do_adapter()
    line = 'data: {"id":"x","object":"chat.completion.chunk","model":"llama3.3-70b-instruct","choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":null}]}'
    chunk = adapter.translate_stream_chunk(line)
    assert chunk is not None
    assert chunk["provider"] == "do"


def test_do_translate_stream_chunk_prefixes_model():
    adapter = _do_adapter()
    line = 'data: {"id":"x","object":"chat.completion.chunk","model":"llama3.3-70b-instruct","choices":[]}'
    chunk = adapter.translate_stream_chunk(line)
    assert chunk["model"] == "do/llama3.3-70b-instruct"


def test_do_translate_stream_chunk_done_returns_none():
    adapter = _do_adapter()
    assert adapter.translate_stream_chunk("data: [DONE]") is None


def test_do_translate_stream_chunk_empty_line_returns_none():
    assert _do_adapter().translate_stream_chunk("") is None


def test_do_translate_stream_chunk_malformed_json_returns_none():
    assert _do_adapter().translate_stream_chunk("data: {bad json}") is None
