"""POST /v1/chat/completions — unified inference endpoint (F1 + F2)."""
from __future__ import annotations

import json

from flask import Blueprint, request, jsonify, g, current_app, Response, stream_with_context

from ..errors import RouterError, error_response
from ..middleware import require_scope
from ..streaming.sse import SSE_DONE, sse_event

chat_bp = Blueprint("chat", __name__)

_ALLOWED_FIELDS = frozenset(
    {"model", "messages", "temperature", "top_p", "max_tokens", "stream", "stop", "user", "metadata"}
)
_VALID_ROLES = frozenset({"system", "user", "assistant", "tool"})


def _validate(body: dict) -> str | None:
    """Pure validation. Returns an error string or None if valid."""
    if not isinstance(body, dict):
        return "Body must be a JSON object"

    if not body.get("model"):
        return "Field 'model' is required"

    messages = body.get("messages")
    if not messages or not isinstance(messages, list) or len(messages) == 0:
        return "Field 'messages' is required and must be a non-empty array"

    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            return f"messages[{i}] must be an object"
        if msg.get("role") not in _VALID_ROLES:
            return f"messages[{i}].role must be one of: {', '.join(sorted(_VALID_ROLES))}"
        if not isinstance(msg.get("content", ""), str):
            return f"messages[{i}].content must be a string"

    temperature = body.get("temperature")
    if temperature is not None:
        if not isinstance(temperature, (int, float)) or not (0 <= temperature <= 2):
            return "temperature must be a number between 0 and 2"

    top_p = body.get("top_p")
    if top_p is not None:
        if not isinstance(top_p, (int, float)) or not (0 <= top_p <= 1):
            return "top_p must be a number between 0 and 1"

    max_tokens = body.get("max_tokens")
    if max_tokens is not None:
        if not isinstance(max_tokens, int) or max_tokens <= 0:
            return "max_tokens must be a positive integer"

    return None


@chat_bp.post("/v1/chat/completions")
@require_scope("chat")
def chat_completions():
    request_id: str = g.request_id

    body = request.get_json(silent=True)
    if body is None:
        return error_response("invalid_request", "Request body must be valid JSON with Content-Type: application/json", 400)

    # Apply default model if caller omitted it
    if isinstance(body, dict) and not body.get("model"):
        default_model = f"do/{current_app.config['DO_DEFAULT_MODEL']}"
        body = {**body, "model": default_model}

    err = _validate(body)
    if err:
        return error_response("invalid_request", err, 400)

    # Strip unknown fields before routing
    payload = {k: v for k, v in body.items() if k in _ALLOWED_FIELDS}

    orchestrator = current_app.extensions["orchestrator"]

    if body.get("stream") is True:
        return _handle_stream(payload, request_id, orchestrator)

    # ── Non-streaming path (F1 + F3) ─────────────────────────────────
    try:
        unified, provider, native_model, latency_ms, attempts = orchestrator.dispatch(payload, request_id)
    except RouterError as exc:
        return error_response(exc.code, exc.message, exc.http_status, exc.error_type)

    resp = jsonify(unified)
    resp.headers["X-Request-Id"] = request_id
    resp.headers["X-Router-Provider"] = provider
    resp.headers["X-Router-Model"] = native_model
    resp.headers["X-Router-Latency-Ms"] = str(latency_ms)
    resp.headers["X-Router-Attempts"] = str(len(attempts))
    resp.headers["X-Router-Fallback-Chain"] = orchestrator.get_chain_str(payload["model"])
    return resp, 200


def _handle_stream(payload: dict, request_id: str, orchestrator) -> Response:
    """SSE streaming path (F2).

    Pre-validates model existence so we can still return a proper HTTP error
    response before committing to text/event-stream.
    """
    unified_model = payload.get("model", "")
    routing = orchestrator.get_routing_info(unified_model)
    if routing is None:
        return error_response("model_not_found", f"Unknown model: '{unified_model}'", 400)

    provider_name, native_model = routing

    def event_generator():
        try:
            for chunk in orchestrator.dispatch_stream(payload, request_id):
                yield sse_event(chunk)
        except Exception as exc:
            # Unexpected pre-stream error that slipped past the pre-check.
            # Client already got 200 — emit an error chunk then close cleanly.
            import sys
            print(f"[stream error] {type(exc).__name__}: {exc}", file=sys.stderr)
            yield sse_event({"object": "chat.completion.chunk", "error": {"code": "upstream_error", "message": str(exc)}})
        yield SSE_DONE

    headers = {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
        "X-Request-Id": request_id,
        "X-Router-Provider": provider_name,
        "X-Router-Model": native_model,
        "X-Router-Fallback-Chain": orchestrator.get_chain_str(unified_model),
    }
    return Response(stream_with_context(event_generator()), status=200, headers=headers)
