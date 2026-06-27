"""Unit tests for CircuitBreaker state machine with injectable clock."""
from __future__ import annotations

from app.router.circuit_breaker import CircuitBreaker


def _breaker(window=10, min_samples=5, rate=0.5, cooldown=30.0, t=None):
    tick = [0.0] if t is None else t
    return CircuitBreaker(window, min_samples, rate, cooldown, clock=lambda: tick[0]), tick


# ── Initial state ─────────────────────────────────────────────────────────────

def test_new_target_is_closed():
    cb, _ = _breaker()
    assert cb.get_state("p", "m") == CircuitBreaker.CLOSED
    assert not cb.is_open("p", "m")


# ── Failure threshold ─────────────────────────────────────────────────────────

def test_below_min_samples_stays_closed():
    cb, _ = _breaker(window=10, min_samples=5, rate=0.5)
    for _ in range(4):  # Below min_samples=5
        cb.record_failure("p", "m")
    assert cb.get_state("p", "m") == CircuitBreaker.CLOSED


def test_at_threshold_opens():
    cb, _ = _breaker(window=10, min_samples=5, rate=0.5)
    for _ in range(5):  # 5/5 = 100% failures
        cb.record_failure("p", "m")
    assert cb.get_state("p", "m") == CircuitBreaker.OPEN
    assert cb.is_open("p", "m")


def test_below_rate_stays_closed():
    cb, _ = _breaker(window=10, min_samples=5, rate=0.6)
    # 2 failures + 3 successes = 40% < 60%
    for _ in range(2):
        cb.record_failure("p", "m")
    for _ in range(3):
        cb.record_success("p", "m")
    assert cb.get_state("p", "m") == CircuitBreaker.CLOSED


def test_above_rate_opens():
    cb, _ = _breaker(window=10, min_samples=5, rate=0.5)
    # 1 success + 4 failures = 80% failures > 50%; last event is a failure → triggers check
    cb.record_success("p", "m")
    for _ in range(4):
        cb.record_failure("p", "m")
    assert cb.get_state("p", "m") == CircuitBreaker.OPEN


# ── Open → Half-open transition ───────────────────────────────────────────────

def test_open_becomes_half_open_after_cooldown():
    tick = [0.0]
    cb, _ = _breaker(cooldown=30.0, t=tick)
    for _ in range(5):
        cb.record_failure("p", "m")
    assert cb.is_open("p", "m")

    tick[0] = 31.0  # Past cooldown
    assert not cb.is_open("p", "m")  # Allows probe
    assert cb.get_state("p", "m") == CircuitBreaker.HALF_OPEN


def test_open_before_cooldown_stays_open():
    tick = [0.0]
    cb, _ = _breaker(cooldown=30.0, t=tick)
    for _ in range(5):
        cb.record_failure("p", "m")
    tick[0] = 29.0  # Before cooldown
    assert cb.is_open("p", "m")


# ── Half-open → Closed (success) ─────────────────────────────────────────────

def test_half_open_success_closes_breaker():
    tick = [0.0]
    cb, _ = _breaker(cooldown=30.0, t=tick)
    for _ in range(5):
        cb.record_failure("p", "m")
    tick[0] = 31.0
    cb.is_open("p", "m")  # Trigger HALF_OPEN transition
    cb.record_success("p", "m")
    assert cb.get_state("p", "m") == CircuitBreaker.CLOSED
    assert not cb.is_open("p", "m")


# ── Half-open → Open (failure, doubled cooldown) ──────────────────────────────

def test_half_open_failure_reopens_with_doubled_cooldown():
    tick = [0.0]
    cb, _ = _breaker(cooldown=30.0, t=tick)
    for _ in range(5):
        cb.record_failure("p", "m")
    tick[0] = 31.0
    cb.is_open("p", "m")  # HALF_OPEN
    cb.record_failure("p", "m")  # Probe failed
    assert cb.get_state("p", "m") == CircuitBreaker.OPEN

    # Cooldown is now 60s; opened_at=31; 61-31=30 < 60 → still OPEN
    tick[0] = 61.0
    assert cb.is_open("p", "m")  # Still blocked

    # 92-31=61 > 60 → transition to HALF_OPEN
    tick[0] = 92.0
    assert not cb.is_open("p", "m")
    assert cb.get_state("p", "m") == CircuitBreaker.HALF_OPEN


def test_half_open_cooldown_capped_at_300s():
    tick = [0.0]
    cb, _ = _breaker(cooldown=200.0, t=tick)  # Start with 200s cooldown
    for _ in range(5):
        cb.record_failure("p", "m")
    tick[0] = 201.0
    cb.is_open("p", "m")  # HALF_OPEN
    cb.record_failure("p", "m")  # Doubles: 200 * 2 = 400 → capped at 300
    # New cooldown is 300s; opened_at = 201
    tick[0] = 501.0  # 501 - 201 = 300 → just enough
    assert not cb.is_open("p", "m")


# ── Isolation: separate targets ───────────────────────────────────────────────

def test_targets_are_independent():
    cb, _ = _breaker()
    for _ in range(5):
        cb.record_failure("p1", "m1")
    assert cb.get_state("p1", "m1") == CircuitBreaker.OPEN
    assert cb.get_state("p2", "m2") == CircuitBreaker.CLOSED


# ── Success resets closed window ─────────────────────────────────────────────

def test_success_after_recovery_resets_window():
    tick = [0.0]
    cb, _ = _breaker(cooldown=30.0, t=tick)
    for _ in range(5):
        cb.record_failure("p", "m")
    tick[0] = 31.0
    cb.is_open("p", "m")  # HALF_OPEN
    cb.record_success("p", "m")  # CLOSED — window cleared
    # Now record fresh failures; should not re-open immediately
    for _ in range(4):
        cb.record_failure("p", "m")
    assert cb.get_state("p", "m") == CircuitBreaker.CLOSED
