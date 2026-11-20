"""One daily AI budget shared by every service that calls a model.

Before this, only Chat's answers counted against the daily ceiling; Knowledge
embeddings and Summariser drafts were metered (Prometheus) but never stopped.
Now every model call records its tokens and (notional) cost in one ledger, and
every service checks the SAME day total before it spends.

The ledger is a small SQLite file on a volume mounted into the three AI-calling
containers (chat, knowledge, summariser). That is a deliberate exception to
database-per-service: the budget is a cross-cutting limit, not domain data, and a
file avoids adding a network dependency to the answer path. Its limits: one host
only, and two services can both pass the check a moment before the ceiling and
overshoot it by one call each (check-then-spend, no reservation).

On the cap, callers degrade instead of failing:
  chat       -> escalate to a person (reason daily_budget_exhausted)
  summariser -> rule-based draft (no LLM)
  knowledge  -> skip the embedding (document stored, indexed later by a rebuild)
and ai_budget_denied_total{service,purpose} is incremented.
"""
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone

from prometheus_client import Counter, Gauge

logger = logging.getLogger("helix.budget")

BUDGET_DB = os.getenv("AI_BUDGET_DB", "/budget/ai_budget.db")
DAILY_COST_CEILING_USD = float(os.getenv("DAILY_COST_CEILING_USD", "0.50"))
DAILY_TOKEN_CEILING = int(os.getenv("DAILY_TOKEN_CEILING", "200000"))

DENIED = Counter("ai_budget_denied_total", "Model calls skipped because the shared daily AI budget is used up",
                 ["service", "purpose"])
SPEND_TODAY = Gauge("ai_budget_spend_today_usd", "Notional AI spend today (UTC), all services")
TOKENS_TODAY = Gauge("ai_budget_tokens_today", "AI tokens used today (UTC), all services")
CEILING = Gauge("ai_budget_ceiling_usd", "Shared daily AI cost ceiling")
CEILING.set(DAILY_COST_CEILING_USD)

_DDL = """
CREATE TABLE IF NOT EXISTS spend (
  day      TEXT NOT NULL,          -- UTC date, YYYY-MM-DD
  service  TEXT NOT NULL,
  purpose  TEXT NOT NULL,          -- answer | summary | index | query ...
  model    TEXT NOT NULL,
  tokens   INTEGER NOT NULL,
  cost_usd REAL NOT NULL,
  at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS spend_by_day ON spend(day);
"""


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(BUDGET_DB) or ".", exist_ok=True)
    conn = sqlite3.connect(BUDGET_DB, timeout=10, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(_DDL)
    return conn


def today() -> tuple[int, float]:
    """(tokens, cost_usd) used today by ALL services."""
    conn = _connect()
    try:
        tokens, cost = conn.execute("SELECT COALESCE(SUM(tokens),0), COALESCE(SUM(cost_usd),0) FROM spend "
                                    "WHERE day=?", (_today(),)).fetchone()
    finally:
        conn.close()
    SPEND_TODAY.set(cost)
    TOKENS_TODAY.set(tokens)
    return int(tokens), float(cost)


def by_service() -> dict[str, dict]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT service, SUM(tokens), SUM(cost_usd) FROM spend WHERE day=? GROUP BY service",
                            (_today(),)).fetchall()
    finally:
        conn.close()
    return {s: {"tokens": int(t), "cost_usd": round(c, 6)} for s, t, c in rows}


def allow(service: str, purpose: str) -> bool:
    """True if the shared budget still has room today. Counts a denial when it does not."""
    tokens, cost = today()
    if cost >= DAILY_COST_CEILING_USD or tokens >= DAILY_TOKEN_CEILING:
        DENIED.labels(service, purpose).inc()
        logger.warning(f"[budget] {service}/{purpose} denied: today {tokens} tokens, ${cost:.6f} "
              f"(ceiling {DAILY_TOKEN_CEILING} tokens / ${DAILY_COST_CEILING_USD})")
        return False
    return True


def record(service: str, purpose: str, model: str, tokens: int, cost_usd: float) -> None:
    """Add one model call to today's total. Never raises: metering must not break the request."""
    try:
        conn = _connect()
        try:
            conn.execute("INSERT INTO spend(day, service, purpose, model, tokens, cost_usd, at) VALUES (?,?,?,?,?,?,?)",
                         (_today(), service, purpose, model, int(tokens), float(cost_usd), time.time()))
        finally:
            conn.close()
        today()
    except Exception as exc:                                  # noqa: BLE001
        logger.warning(f"[budget] could not record spend: {exc!r}")
