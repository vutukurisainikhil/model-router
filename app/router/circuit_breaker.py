"""Lightweight in-process circuit breaker (per provider+model target).

State machine: CLOSED → OPEN → HALF_OPEN → CLOSED
- CLOSED  : normal operation; failures recorded in sliding window.
- OPEN    : target is skipped; after cooldown → HALF_OPEN.
- HALF_OPEN: one probe allowed; success → CLOSED, failure → OPEN (doubled cooldown).

The clock is injectable so unit tests can fast-forward time deterministically.
"""
from __future__ import annotations

import threading
from collections import deque


class CircuitBreaker:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        window_size: int = 20,
        min_samples: int = 10,
        failure_rate_open: float = 0.5,
        cooldown_s: float = 30.0,
        clock=None,
    ) -> None:
        import time
        self._window_size = window_size
        self._min_samples = min_samples
        self._failure_rate_open = failure_rate_open
        self._cooldown_s = cooldown_s
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._state: dict[tuple, dict] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_open(self, provider: str, model: str) -> bool:
        """Return True if this target should be skipped (breaker is OPEN)."""
        key = (provider, model)
        with self._lock:
            s = self._state.get(key)
            if not s or s["state"] == self.CLOSED:
                return False
            if s["state"] == self.OPEN:
                if self._clock() - s["opened_at"] >= s["cooldown_s"]:
                    s["state"] = self.HALF_OPEN
                    return False  # Allow the probe
                return True
            # HALF_OPEN: allow one probe
            return False

    def record_success(self, provider: str, model: str) -> None:
        key = (provider, model)
        with self._lock:
            s = self._get_or_create(key)
            if s["state"] in (self.OPEN, self.HALF_OPEN):
                # Recovery: reset to CLOSED
                s["state"] = self.CLOSED
                s["window"].clear()
                s["cooldown_s"] = self._cooldown_s
            else:
                s["window"].append(0)  # 0 = success

    def record_failure(self, provider: str, model: str) -> None:
        key = (provider, model)
        with self._lock:
            s = self._get_or_create(key)
            if s["state"] == self.HALF_OPEN:
                # Probe failed: re-open with exponential back-off
                s["cooldown_s"] = min(s["cooldown_s"] * 2, 300.0)
                s["state"] = self.OPEN
                s["opened_at"] = self._clock()
                return
            if s["state"] == self.OPEN:
                return  # Already open
            # CLOSED: update sliding window
            s["window"].append(1)  # 1 = failure
            if len(s["window"]) >= self._min_samples:
                failure_rate = sum(s["window"]) / len(s["window"])
                if failure_rate >= self._failure_rate_open:
                    s["state"] = self.OPEN
                    s["opened_at"] = self._clock()

    def get_state(self, provider: str, model: str) -> str:
        key = (provider, model)
        with self._lock:
            s = self._state.get(key)
            return s["state"] if s else self.CLOSED

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_or_create(self, key: tuple) -> dict:
        if key not in self._state:
            self._state[key] = {
                "state": self.CLOSED,
                "window": deque(maxlen=self._window_size),
                "opened_at": 0.0,
                "cooldown_s": self._cooldown_s,
            }
        return self._state[key]
