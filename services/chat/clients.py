"""Outbound calls from Chat, each with a hard per-attempt deadline and the shared
retry policy (common/retry.py: exponential backoff with jitter, max attempts, budget).

Knowledge Base calls also go through a circuit breaker: when the KB is down we
stop waiting on it (fail fast) and the caller escalates instead of guessing.
"""
import logging
import os

import httpx

from common import log, retry
from common.circuit_breaker import CircuitBreaker, CircuitOpenError
from common.retry import AttemptTimeout

logger = logging.getLogger("chat.clients")

KNOWLEDGE_URL = os.getenv("KNOWLEDGE_URL", "http://localhost:8004")
TICKETS_URL = os.getenv("TICKETS_URL", "http://localhost:8002")
KB_TIMEOUT = float(os.getenv("KB_TIMEOUT", "2.0"))
TICKETS_TIMEOUT = float(os.getenv("TICKETS_TIMEOUT", "3.0"))
RETRIES = int(os.getenv("RETRIES", "2"))           # retries AFTER the first attempt
BACKOFF_BASE = float(os.getenv("BACKOFF_BASE", "0.2"))
KB_BUDGET = float(os.getenv("KB_BUDGET", "3.0"))            # total seconds for all KB attempts
TICKETS_BUDGET = float(os.getenv("TICKETS_BUDGET", "6.0"))

kb_breaker = CircuitBreaker(
    "chat->knowledge",
    failure_threshold=int(os.getenv("CB_FAILURE_THRESHOLD", "3")),
    window_size=int(os.getenv("CB_WINDOW_SIZE", "10")),
    open_timeout=float(os.getenv("CB_OPEN_TIMEOUT", "15")),
)


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 502, 503, 504)
    return isinstance(exc, (httpx.TransportError, AttemptTimeout))


def _log(msg: str) -> None:
    logger.warning(msg)


def search_kb(query: str, top_k: int = 3) -> dict:
    """Raises CircuitOpenError if the breaker is open, or the last error after retries."""
    if not kb_breaker.allow_request():
        raise CircuitOpenError("knowledge circuit is OPEN")

    def once():
        r = httpx.post(f"{KNOWLEDGE_URL}/search", json={"query": query, "top_k": top_k},
                       headers=log.headers(), timeout=KB_TIMEOUT)
        r.raise_for_status()
        return r.json()

    try:
        result = retry.call(once, attempts=RETRIES + 1, base=BACKOFF_BASE, cap=1.0,
                            attempt_timeout=KB_TIMEOUT, deadline=KB_BUDGET,
                            retryable=_retryable, label="kb", log=_log)
    except Exception:
        kb_breaker.record_failure()
        raise
    kb_breaker.record_success()
    return result


def open_ticket(idempotency_key: str, payload: dict) -> dict:
    """Idempotent: retries reuse the same Idempotency-Key, so at most one ticket is created."""
    def once():
        r = httpx.post(f"{TICKETS_URL}/tickets", json=payload,
                       headers={**log.headers(), "Idempotency-Key": idempotency_key}, timeout=TICKETS_TIMEOUT)
        r.raise_for_status()
        return r.json()

    return retry.call(once, attempts=RETRIES + 1, base=BACKOFF_BASE, cap=1.0,
                      attempt_timeout=TICKETS_TIMEOUT, deadline=TICKETS_BUDGET,
                      retryable=_retryable, label="tickets", log=_log)
