"""Orchestrator: resolves model, runs fallback attempt loop, returns unified response.

F1: schema translation.
F2: streaming via dispatch_stream().
F3: fallback chain + circuit breaker.
"""
from __future__ import annotations

import time
import uuid
from typing import Generator

import httpx

from ..adapters.base import BaseAdapter
from ..errors import RouterError
from ..logging_setup import log
from .circuit_breaker import CircuitBreaker
from .model_registry import ModelRegistry

# ── Error classification ──────────────────────────────────────────────────────

_RETRYABLE_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})
_NON_RETRYABLE_HTTP = frozenset({400, 401, 403})


def _is_retryable(status: int) -> bool:
    if status in _NON_RETRYABLE_HTTP:
        return False
    return status in _RETRYABLE_HTTP or status >= 500


def _error_code(status: int) -> str:
    if status == 429:
        return "rate_limited"
    if status in (408, 504):
        return "upstream_timeout"
    return "upstream_error"


def _client_status(status: int) -> int:
    if status == 429:
        return 429
    if status in (408, 504):
        return 408
    return 502


def _error_chunk() -> dict:
    return {
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
    }


# ── Orchestrator ──────────────────────────────────────────────────────────────


class Orchestrator:
    def __init__(
        self,
        registry: ModelRegistry,
        adapters: dict[str, BaseAdapter],
        breaker: CircuitBreaker | None = None,
        request_deadline_s: float = 60.0,
        max_fallbacks: int = 3,
    ) -> None:
        self._registry = registry
        self._adapters = adapters
        self._breaker = breaker or CircuitBreaker()
        self._request_deadline_s = request_deadline_s
        self._max_fallbacks = max_fallbacks

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def get_routing_info(self, unified_model: str) -> tuple[str, str] | None:
        """Return (provider_name, native_model) or None if model unknown."""
        entry = self._registry.resolve(unified_model)
        if entry is None:
            return None
        p = entry["primary"]
        return p["provider"], p["model"]

    def get_chain_str(self, unified_model: str) -> str:
        """Comma-separated 'provider/model' string for the full fallback chain."""
        entry = self._registry.resolve(unified_model)
        if entry is None:
            return unified_model
        return ",".join(
            f"{t['provider']}/{t['model']}" for t in self._build_chain(entry)
        )

    def _build_chain(self, entry: dict) -> list[dict]:
        chain = [entry["primary"]] + entry.get("fallbacks", [])
        return chain[: self._max_fallbacks + 1]

    def _get_adapter(self, provider: str) -> BaseAdapter | None:
        return self._adapters.get(provider)

    # ------------------------------------------------------------------
    # Non-streaming dispatch (F1 + F3)
    # ------------------------------------------------------------------

    def dispatch(
        self, unified_payload: dict, request_id: str
    ) -> tuple[dict, str, str, int, list]:
        """
        Attempt loop with fallback chain.
        Returns (unified, provider, native_model, latency_ms, attempts).
        Raises RouterError when chain is exhausted or non-retryable error hits.
        """
        unified_model = unified_payload.get("model", "")
        entry = self._registry.resolve(unified_model)
        if entry is None:
            raise RouterError("model_not_found", f"Unknown model: '{unified_model}'", 400)

        chain = self._build_chain(entry)
        deadline = time.monotonic() + self._request_deadline_s
        attempts: list[dict] = []
        last_code = "upstream_error"
        last_status = 502

        for target in chain:
            if time.monotonic() >= deadline:
                break

            provider = target["provider"]
            model = target["model"]

            if self._breaker.is_open(provider, model):
                attempts.append({"provider": provider, "model": model, "outcome": "skipped_breaker"})
                continue

            adapter = self._get_adapter(provider)
            if adapter is None:
                continue

            native_payload = adapter.translate_request(unified_payload)
            t0 = time.monotonic()

            try:
                raw = adapter.call(native_payload, stream=False)
                latency_ms = round((time.monotonic() - t0) * 1000)
                self._breaker.record_success(provider, model)
                unified = adapter.translate_response(raw)
                unified.setdefault("id", f"chatcmpl_{uuid.uuid4().hex}")
                unified.setdefault("object", "chat.completion")
                attempts.append({"provider": provider, "model": model, "outcome": "success", "latency_ms": latency_ms})
                log.info("dispatch_complete", extra={
                    "request_id": request_id, "unified_model": unified_model,
                    "served_by": provider, "native_model": model,
                    "total_latency_ms": latency_ms, "attempts": attempts,
                })
                return unified, provider, model, latency_ms, attempts

            except httpx.HTTPStatusError as exc:
                latency_ms = round((time.monotonic() - t0) * 1000)
                status = exc.response.status_code if exc.response else 502
                self._breaker.record_failure(provider, model)
                attempts.append({"provider": provider, "model": model, "outcome": "error",
                                  "error_class": f"http_{status}", "latency_ms": latency_ms})
                if not _is_retryable(status):
                    raise RouterError(_error_code(status), f"Provider returned HTTP {status}",
                                      _client_status(status), "upstream") from exc
                last_code, last_status = _error_code(status), _client_status(status)

            except httpx.TimeoutException as exc:
                latency_ms = round((time.monotonic() - t0) * 1000)
                self._breaker.record_failure(provider, model)
                attempts.append({"provider": provider, "model": model, "outcome": "error",
                                  "error_class": "timeout", "latency_ms": latency_ms})
                last_code, last_status = "upstream_timeout", 408

            except httpx.ConnectError as exc:
                latency_ms = round((time.monotonic() - t0) * 1000)
                self._breaker.record_failure(provider, model)
                attempts.append({"provider": provider, "model": model, "outcome": "error",
                                  "error_class": "connect_error", "latency_ms": latency_ms})
                last_code, last_status = "upstream_error", 502

            except Exception as exc:
                latency_ms = round((time.monotonic() - t0) * 1000)
                self._breaker.record_failure(provider, model)
                attempts.append({"provider": provider, "model": model, "outcome": "error",
                                  "error_class": type(exc).__name__, "latency_ms": latency_ms})
                last_code, last_status = "upstream_error", 502

        raise RouterError(last_code, "All upstream providers failed", last_status, "upstream")

    # ------------------------------------------------------------------
    # Streaming dispatch (F2 + F3)
    # ------------------------------------------------------------------

    def dispatch_stream(
        self, unified_payload: dict, request_id: str
    ) -> Generator[dict, None, None]:
        """
        Generator yielding unified chunk dicts.

        F3 fallback rules:
        - Retryable error BEFORE first chunk  → transparent fallback to next target.
        - Non-retryable error before first chunk → error chunk + stop.
        - Any error AFTER first chunk (mid-stream) → error chunk + stop, no fallback.
        - Client disconnect (GeneratorExit) → clean close of upstream connection.
        """
        unified_model = unified_payload.get("model", "")
        entry = self._registry.resolve(unified_model)
        if entry is None:
            raise RouterError("model_not_found", f"Unknown model: '{unified_model}'", 400)

        chain = self._build_chain(entry)
        deadline = time.monotonic() + self._request_deadline_s

        for target in chain:
            if time.monotonic() >= deadline:
                break

            provider = target["provider"]
            model_name = target["model"]

            if self._breaker.is_open(provider, model_name):
                continue

            adapter = self._get_adapter(provider)
            if adapter is None:
                continue

            native_payload = adapter.translate_request(unified_payload)
            first_chunk_sent = False
            line_iter = None

            try:
                line_iter = adapter.call(native_payload, stream=True)
                for line in line_iter:
                    if not line:
                        continue
                    if isinstance(line, str):
                        line = line.strip()
                    if line in ("data: [DONE]", "[DONE]"):
                        self._breaker.record_success(provider, model_name)
                        return
                    chunk = adapter.translate_stream_chunk(line)
                    if chunk is None:
                        continue
                    first_chunk_sent = True
                    yield chunk

                self._breaker.record_success(provider, model_name)
                return

            except GeneratorExit:
                return  # Client disconnected; finally block closes line_iter

            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response else 502
                self._breaker.record_failure(provider, model_name)
                if first_chunk_sent:
                    yield _error_chunk()
                    return
                if not _is_retryable(status):
                    yield _error_chunk()
                    return
                # Retryable pre-first-byte: fall through to next target

            except (httpx.TimeoutException, httpx.ConnectError):
                self._breaker.record_failure(provider, model_name)
                if first_chunk_sent:
                    yield _error_chunk()
                    return
                # Retryable: next target

            except Exception:
                self._breaker.record_failure(provider, model_name)
                if first_chunk_sent:
                    yield _error_chunk()
                    return
                # Next target

            finally:
                # Always release the upstream connection
                if line_iter is not None and hasattr(line_iter, "close"):
                    try:
                        line_iter.close()
                    except Exception:
                        pass

        # All targets exhausted before first chunk
        yield _error_chunk()
