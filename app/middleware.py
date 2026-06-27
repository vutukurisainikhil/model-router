"""Request-level middleware: request-id injection, auth, body-size guard."""
from __future__ import annotations

import uuid
from flask import Flask, request, g, current_app

from .errors import error_response


def register_middleware(app: Flask) -> None:

    @app.before_request
    def _assign_request_id() -> None:
        """Always first — guarantees g.request_id exists for all subsequent handlers."""
        g.request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())

    @app.before_request
    def _authenticate():
        if request.path == "/health":
            return None
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return error_response("unauthorized", "Missing or invalid Authorization header", 401)
        token = auth_header[7:].strip()
        if token not in current_app.config["ROUTER_API_KEYS"]:
            return error_response("unauthorized", "Invalid API key", 401)
        return None

    @app.before_request
    def _check_body_size():
        if request.path == "/health":
            return None
        content_length = request.content_length
        max_bytes = current_app.config.get("MAX_BODY_BYTES", 256 * 1024)
        if content_length and content_length > max_bytes:
            return error_response("invalid_request", "Request body exceeds maximum allowed size", 413)
        return None
