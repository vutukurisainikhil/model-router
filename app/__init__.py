"""Application factory for the Unified Model Router service."""
from __future__ import annotations

from flask import Flask

from .config import Config
from .logging_setup import setup_logging
from .middleware import register_middleware
from .routes.health import health_bp
from .routes.chat import chat_bp
from .adapters.do import DOAdapter
from .adapters.mock import MockAdapter
from .router.circuit_breaker import CircuitBreaker
from .router.model_registry import build_registry, ModelRegistry
from .router.orchestrator import Orchestrator


def create_app(config: Config | None = None) -> Flask:
    setup_logging()
    app = Flask(__name__)

    cfg = config or Config()

    # Populate Flask config (never expose the DO key in plain config dict)
    app.config["SERVICE_NAME"] = "unified-model-router"
    app.config["SERVICE_VERSION"] = cfg.SERVICE_VERSION
    app.config["ENV"] = cfg.APP_ENV
    app.config["ROUTER_API_KEYS"] = cfg.ROUTER_API_KEYS
    app.config["ROUTER_KEY_SCOPES"] = cfg.ROUTER_KEY_SCOPES
    app.config["MAX_BODY_BYTES"] = cfg.MAX_BODY_BYTES
    app.config["DO_DEFAULT_MODEL"] = cfg.DO_DEFAULT_MODEL

    # Build adapters
    do_adapter = DOAdapter(cfg.DO_INFERENCE_BASE_URL, cfg.DO_INFERENCE_API_KEY)
    mock_adapter = MockAdapter()
    adapters: dict = {"do": do_adapter, "mock": mock_adapter}

    # Build registry + orchestrator and stash on app.extensions
    registry = ModelRegistry(build_registry(cfg.DO_DEFAULT_MODEL))
    breaker = CircuitBreaker()
    app.extensions["orchestrator"] = Orchestrator(registry, adapters, breaker=breaker)

    # Middleware (order matters: request_id → auth → body-size)
    register_middleware(app)

    # Blueprints
    app.register_blueprint(health_bp)
    app.register_blueprint(chat_bp)

    return app
