"""In-process mock adapter — deterministic, no network, used in tests and offline dev."""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Generator

from .base import BaseAdapter
from ..streaming.sse import parse_sse_line


class MockAdapter(BaseAdapter):
    name = "mock"

    def translate_request(self, unified: dict) -> dict:
        return dict(unified)

    def call(self, native_payload: dict, *, stream: bool) -> Any:
        if stream:
            return self._stream_lines(native_payload)

        user_content = ""
        for msg in native_payload.get("messages", []):
            if msg.get("role") == "user":
                user_content = msg.get("content", "")
                break

        prompt_tokens = max(1, len(user_content.split()))
        reply = f"[mock] echo: {user_content}"
        completion_tokens = len(reply.split())

        return {
            "_mock": True,
            "id": f"chatcmpl_{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": native_payload.get("model", "mock/echo"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": reply},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    def _stream_lines(self, payload: dict) -> Generator[str, None, None]:
        """Yield SSE-formatted lines that mirror what a real provider sends."""
        user_content = ""
        for msg in payload.get("messages", []):
            if msg.get("role") == "user":
                user_content = msg.get("content", "")
                break

        words = f"[mock stream] {user_content}".split()
        prompt_tokens = max(1, len(user_content.split()))
        chunk_id = f"chatcmpl_{uuid.uuid4().hex[:12]}"
        model = payload.get("model", "mock/echo")
        created = int(time.time())

        def _line(delta: dict, finish_reason: str | None = None, usage: dict | None = None) -> str:
            chunk: dict = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
            if usage:
                chunk["usage"] = usage
            return f"data: {json.dumps(chunk)}"

        # First chunk: role
        yield _line({"role": "assistant"})
        # Content chunks: one word each
        for word in words:
            yield _line({"content": word + " "})
        # Final chunk: finish_reason + usage
        completion_tokens = len(words)
        yield _line(
            {},
            finish_reason="stop",
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        )
        yield "data: [DONE]"

    def translate_response(self, native_response: Any) -> dict:
        result = dict(native_response)
        result["provider"] = "mock"
        model = result.get("model", "echo")
        if not model.startswith("mock/"):
            result["model"] = f"mock/{model}"
        # Remove internal flag
        result.pop("_mock", None)
        return result

    def translate_stream_chunk(self, line: str) -> dict | None:
        """Parse one SSE data line → unified chunk dict, or None to skip."""
        json_str, is_done = parse_sse_line(line)
        if is_done or not json_str:
            return None
        try:
            chunk = json.loads(json_str)
        except (json.JSONDecodeError, ValueError):
            return None
        chunk["provider"] = "mock"
        model = chunk.get("model", "echo")
        if not model.startswith("mock/"):
            chunk["model"] = f"mock/{model}"
        return chunk
