"""Unified error types and response helpers."""
from __future__ import annotations

from flask import jsonify, g


class RouterError(Exception):
    """Raised anywhere in the stack; caught at the route handler."""

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int,
        error_type: str = "router",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.error_type = error_type


def error_response(
    code: str,
    message: str,
    http_status: int,
    error_type: str = "router",
):
    """Build the unified error envelope and return a (Response, status) tuple."""
    try:
        request_id = getattr(g, "request_id", None)
    except RuntimeError:
        request_id = None

    body = {
        "error": {
            "code": code,
            "message": message,
            "type": error_type,
            "request_id": request_id,
        }
    }
    return jsonify(body), http_status
