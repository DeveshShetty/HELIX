"""One retry policy for every service: exponential backoff with jitter, a maximum
number of attempts, and an optional overall deadline.

Before this module there were three hand-written retry loops (Chat, On-Call,
Summariser), none with jitter, plus fixed-interval infinite loops in the consumer
reconnect, the outbox relay and the escalation worker.

Jitter matters because when a dependency comes back, every caller that was backing
off would otherwise retry at the same instant.
"""
import contextvars
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Callable, TypeVar

T = TypeVar("T")

# Attempts that need a hard deadline run on this pool: httpx timeouts do not cover DNS
# resolution, and resolving a stopped container's name takes ~4 s in Docker. A timed-out
# attempt's thread finishes in the background; the pool bounds how many can pile up.
_pool = ThreadPoolExecutor(max_workers=int(os.getenv("OUTBOUND_POOL_SIZE", "16")), thread_name_prefix="outbound")


class AttemptTimeout(Exception):
    """An attempt did not finish within its hard deadline."""


def backoff(attempt: int, base: float, cap: float) -> float:
    """Delay before retrying after failed attempt number `attempt` (1-based).

    Exponential, capped, with "equal jitter": half the delay is fixed, half random,
    so retries spread out but never collapse to zero.
    """
    d = min(cap, base * 2 ** (attempt - 1))
    return d / 2 + random.uniform(0, d / 2)


def call(fn: Callable[[], T], *, attempts: int = 3, base: float = 0.2, cap: float = 2.0,
         attempt_timeout: float | None = None, deadline: float | None = None,
         retryable: Callable[[Exception], bool] = lambda e: True, label: str = "call",
         log: Callable[[str], None] | None = None, sleep: Callable[[float], None] = time.sleep) -> T:
    """Run fn() with retries. Raises the last error when attempts, retryability or the
    deadline run out. A retry that could not finish inside `deadline` is not started, so
    a hanging dependency costs one attempt_timeout, not `attempts` of them."""
    started = time.monotonic()
    for attempt in range(1, attempts + 1):
        try:
            if attempt_timeout:
                ctx = contextvars.copy_context()          # keep the correlation id in the worker thread
                return _pool.submit(ctx.run, fn).result(timeout=attempt_timeout)
            return fn()
        except FutureTimeout:
            exc: Exception = AttemptTimeout(f"no response within {attempt_timeout}s")
        except Exception as e:                            # noqa: BLE001 - classified by `retryable`
            exc = e
        delay = backoff(attempt, base, cap)
        out_of_time = deadline is not None and \
            time.monotonic() - started + delay + (attempt_timeout or 0) > deadline
        if attempt == attempts or not retryable(exc) or out_of_time:
            raise exc
        if log:
            log(f"[{label}] attempt {attempt}/{attempts} failed ({type(exc).__name__}); retry in {delay:.2f}s")
        sleep(delay)
    raise RuntimeError("unreachable")
