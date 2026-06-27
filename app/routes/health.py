"""Health check endpoints."""
from __future__ import annotations

import time
from flask import Blueprint, current_app, jsonify

health_bp = Blueprint("health", __name__)

_STARTED_AT = time.time()


@health_bp.get("/health")
def health():
    """Liveness probe. Returns 200 if the process is up."""
    return jsonify(
        status="ok",
        service=current_app.config.get("SERVICE_NAME"),
        version=current_app.config.get("SERVICE_VERSION"),
        env=current_app.config.get("ENV"),
        uptime_seconds=round(time.time() - _STARTED_AT, 3),
    ), 200
