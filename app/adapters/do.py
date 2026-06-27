"""DigitalOcean Serverless Inference adapter.

DO's API is OpenAI-compatible at the HTTP level, so translation is minimal:
- Request:  strip "do/" prefix from model id; drop internal "metadata" field.
- Response: add "provider": "do"; prefix model id with "do/".

All models (including anthropic-* and openai-*) use the same DO endpoint;
DO handles vendor translation internally.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from .base import BaseAdapter
from ..streaming.sse import parse_sse_line


class DOAdapter(BaseAdapter):
    name = "do"

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        # api_key intentionally NOT stored as an attribute to reduce accidental
        # exposure in tracebacks/repr; kept inside the client headers only.
        _headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        _limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
        # Non-streaming: shorter read timeout (full response buffered server-side)
        self._client = httpx.Client(
            headers=_headers,
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0),
            limits=_limits,
        )
        # Streaming: generous read-idle timeout; model may pause between tokens
        self._stream_client = httpx.Client(
            headers=_headers,
            timeout=httpx.Timeout(connect=10.0, read=90.0, write=10.0, pool=10.0),
            limits=_limits,
        )

    # ------------------------------------------------------------------
    # Adapter contract
    # ------------------------------------------------------------------

    def translate_request(self, unified: dict) -> dict:
        """Strip do/ prefix and remove router-internal fields."""
        native = {k: v for k, v in unified.items() if k != "metadata"}
        model = native.get("model", "")
        if model.startswith("do/"):
            native["model"] = model[3:]
        return native

    def call(self, native_payload: dict, *, stream: bool) -> Any:
        if stream:
            return self._stream_lines(native_payload)
        payload = dict(native_payload)
        payload["stream"] = False
        url = f"{self._base_url}/chat/completions"
        resp = self._client.post(url, json=payload)
        resp.raise_for_status()
        return resp

    def _stream_lines(self, native_payload: dict):
        """Generator: opens a streaming HTTP connection and yields raw SSE lines.

        raise_for_status() is called before the first yield so that HTTP
        4xx/5xx errors surface immediately (and the orchestrator can classify
        them as pre-first-byte errors eligible for fallback in F3).
        """
        payload = dict(native_payload)
        payload["stream"] = True
        url = f"{self._base_url}/chat/completions"
        with self._stream_client.stream("POST", url, json=payload) as resp:
            resp.raise_for_status()  # before first yield → pre-stream error
            for line in resp.iter_lines():
                if line:
                    yield line

    def translate_response(self, native_response: Any) -> dict:
        data = native_response.json()
        native_model = data.get("model", "")
        data["provider"] = "do"
        if not native_model.startswith("do/"):
            data["model"] = f"do/{native_model}"
        return data

    def translate_stream_chunk(self, line: str) -> dict | None:
        """Parse one SSE data line → unified chunk dict, or None to skip."""
        json_str, is_done = parse_sse_line(line)
        if is_done or not json_str:
            return None
        try:
            chunk = json.loads(json_str)
        except (json.JSONDecodeError, ValueError):
            return None
        chunk["provider"] = "do"
        native_model = chunk.get("model", "")
        if not native_model.startswith("do/"):
            chunk["model"] = f"do/{native_model}"
        return chunk

    def close(self) -> None:
        self._client.close()
        self._stream_client.close()
