"""Count-based sliding-window circuit breaker (adapted from Lab 3).

Changes from the lab version: thread-safe (FastAPI runs sync handlers in a
thread pool), named, and exports its state as a Prometheus gauge.
"""
import logging
import threading
import time
from collections import deque
from enum import Enum

from prometheus_client import Gauge

logger = logging.getLogger("helix.breaker")


CIRCUIT_STATE = Gauge(
    "circuit_breaker_state",
    "Circuit breaker state: CLOSED=0, OPEN=1, HALF_OPEN=2",
    ["breaker"],
)


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


_GAUGE_VALUE = {CircuitState.CLOSED: 0, CircuitState.OPEN: 1, CircuitState.HALF_OPEN: 2}


class CircuitOpenError(Exception):
    """Raised (by callers) when the breaker refuses a request."""


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 5, window_size: int = 10,
                 open_timeout: float = 10.0):
        self.name = name
        self.failure_threshold = failure_threshold
        self.open_timeout = open_timeout
        self.state = CircuitState.CLOSED
        self.history: deque[bool] = deque(maxlen=window_size)
        self.opened_at: float | None = None
        self.half_open_trial = False
        self._lock = threading.Lock()
        CIRCUIT_STATE.labels(name).set(0)

    def _set(self, new_state: CircuitState) -> None:
        if new_state != self.state:
            logger.info(f"[breaker:{self.name}] {self.state.value} -> {new_state.value}")
            self.state = new_state
        CIRCUIT_STATE.labels(self.name).set(_GAUGE_VALUE[new_state])

    def allow_request(self) -> bool:
        with self._lock:
            if self.state == CircuitState.OPEN:
                if time.monotonic() - (self.opened_at or 0) < self.open_timeout:
                    return False
                self._set(CircuitState.HALF_OPEN)
                self.half_open_trial = False
            if self.state == CircuitState.HALF_OPEN:
                if self.half_open_trial:
                    return False  # only one probe at a time
                self.half_open_trial = True
                return True
            return True

    def record_success(self) -> None:
        with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self.history.clear()
                self.opened_at = None
                self.half_open_trial = False
                self._set(CircuitState.CLOSED)
            else:
                self.history.append(True)

    def record_failure(self) -> None:
        with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self.opened_at = time.monotonic()
                self.half_open_trial = False
                self._set(CircuitState.OPEN)
                return
            self.history.append(False)
            if sum(1 for ok in self.history if not ok) >= self.failure_threshold:
                self.opened_at = time.monotonic()
                self._set(CircuitState.OPEN)
