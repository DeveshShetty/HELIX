"""Outbound calls from Chat, each with timeout + retry with exponential backoff.

Knowledge Base calls also go through a circuit breaker: when the KB is down we
stop waiting on it (fail fast) and the caller escalates instead of guessing.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

import httpx

from common.circuit_breaker import CircuitBreaker, CircuitOpenError

KNOWLEDGE_URL = os.getenv("KNOWLEDGE_URL", "http://localhost:8004")
TICKETS_URL = os.getenv("TICKETS_URL", "http://localhost:8002")
KB_TIMEOUT = float(os.getenv("KB_TIMEOUT", "2.0"))
TICKETS_TIMEOUT = float(os.getenv("TICKETS_TIMEOUT", "3.0"))
RETRIES = int(os.getenv("RETRIES", "2"))           # retries AFTER the first attempt
BACKOFF_BASE = float(os.getenv("BACKOFF_BASE", "0.2"))

kb_breaker = CircuitBreaker(
    "chat->knowledge",
    failure_threshold=int(os.getenv("CB_FAILURE_THRESHOLD", "3")),
    window_size=int(os.getenv("CB_WINDOW_SIZE", "10")),
    open_timeout=float(os.getenv("CB_OPEN_TIMEOUT", "15")),
)


KB_BUDGET = float(os.getenv("KB_BUDGET", "3.0"))            # total seconds for all KB attempts
TICKETS_BUDGET = float(os.getenv("TICKETS_BUDGET", "6.0"))

# httpx timeouts do not cover DNS resolution: resolving a stopped container's
# name takes ~4 s in Docker. Running each attempt in a pool lets us enforce a
# hard deadline from the outside. A timed-out attempt's thread finishes in the
# background; the pool bounds how many can pile up.
_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="outbound")


class AttemptTimeout(Exception):
    pass


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 502, 503, 504)
    return isinstance(exc, (httpx.TransportError, AttemptTimeout))


def _with_retry(fn, label: str, attempt_timeout: float, budget: float):
    """Retry with exponential backoff, a hard per-attempt deadline and a total budget.

    A retry is skipped if it could not finish inside the budget, so a hanging
    dependency costs one attempt_timeout, not (RETRIES + 1) of them.
    """
    started = time.monotonic()
    for attempt in range(RETRIES + 1):
        try:
            return _pool.submit(fn).result(timeout=attempt_timeout)
        except FutureTimeout:
            exc: Exception = AttemptTimeout(f"no response within {attempt_timeout}s")
        except Exception as e:
            exc = e
        delay = BACKOFF_BASE * (2 ** attempt)
        out_of_budget = time.monotonic() - started + delay + attempt_timeout > budget
        if attempt == RETRIES or not _retryable(exc) or out_of_budget:
            raise exc
        print(f"[{label}] attempt {attempt + 1} failed ({type(exc).__name__}); retry in {delay:.1f}s", flush=True)
        time.sleep(delay)


def search_kb(query: str, top_k: int = 3) -> dict:
    """Raises CircuitOpenError if the breaker is open, or the last error after retries."""
    if not kb_breaker.allow_request():
        raise CircuitOpenError("knowledge circuit is OPEN")

    def call():
        r = httpx.post(f"{KNOWLEDGE_URL}/search", json={"query": query, "top_k": top_k},
                       timeout=KB_TIMEOUT)
        r.raise_for_status()
        return r.json()

    try:
        result = _with_retry(call, "kb", KB_TIMEOUT, KB_BUDGET)
    except Exception:
        kb_breaker.record_failure()
        raise
    kb_breaker.record_success()
    return result


def open_ticket(idempotency_key: str, payload: dict) -> dict:
    """Idempotent: retries reuse the same Idempotency-Key, so at most one ticket is created."""
    def call():
        r = httpx.post(f"{TICKETS_URL}/tickets", json=payload,
                       headers={"Idempotency-Key": idempotency_key}, timeout=TICKETS_TIMEOUT)
        r.raise_for_status()
        return r.json()

    return _with_retry(call, "tickets", TICKETS_TIMEOUT, TICKETS_BUDGET)
