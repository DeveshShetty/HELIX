"""SQLite helpers + transactional outbox relay (adapted from Lab 2).

Each service owns its own SQLite file (database-per-service). The outbox
table lives in the same file as the service's domain tables so that a state
change and the event announcing it commit in ONE local transaction.
"""
import json
import os
import sqlite3
import threading
import time

import pika
from prometheus_client import Counter

from common import retry
from common.messaging import publish_event

OUTBOX_DDL = """
CREATE TABLE IF NOT EXISTS outbox (
  event_id        TEXT PRIMARY KEY,
  payload         TEXT NOT NULL,
  published       INTEGER NOT NULL DEFAULT 0,   -- 0 pending, 1 published, -1 parked
  created_at      REAL NOT NULL,
  attempts        INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL DEFAULT 0,
  last_error      TEXT
);
CREATE TABLE IF NOT EXISTS processed_events (
  event_id     TEXT PRIMARY KEY,
  processed_at REAL NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)  # explicit BEGINs
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def enqueue(conn: sqlite3.Connection, event: dict) -> None:
    """Write an event to the outbox. Call INSIDE the caller's transaction."""
    conn.execute(
        "INSERT OR IGNORE INTO outbox(event_id, payload, published, created_at) VALUES (?,?,0,?)",
        (event["event_id"], json.dumps(event), time.time()),
    )


def already_processed(conn: sqlite3.Connection, event_id: str) -> bool:
    return conn.execute("SELECT 1 FROM processed_events WHERE event_id=?", (event_id,)).fetchone() is not None


def mark_processed(conn: sqlite3.Connection, event_id: str) -> None:
    """Record an inbound event as handled. Call INSIDE the caller's transaction."""
    conn.execute("INSERT OR IGNORE INTO processed_events(event_id, processed_at) VALUES (?,?)",
                 (event_id, time.time()))


def outbox_backlog(db_path: str) -> int:
    conn = connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM outbox WHERE published=0").fetchone()[0]
    finally:
        conn.close()


OUTBOX_MAX_ATTEMPTS = int(os.getenv("OUTBOX_MAX_ATTEMPTS", "10"))
PARKED = -1          # outbox.published value for an event that kept failing: needs a person

OUTBOX_PARKED = Counter("outbox_parked_total", "Outbox events parked after repeated publish failures", ["db"])


def migrate_outbox(conn: sqlite3.Connection) -> None:
    """Add the per-event retry columns to outbox tables created before they existed."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(outbox)")}
    for name, ddl in (("attempts", "INTEGER NOT NULL DEFAULT 0"), ("next_attempt_at", "REAL NOT NULL DEFAULT 0"),
                      ("last_error", "TEXT")):
        if name not in cols:
            conn.execute(f"ALTER TABLE outbox ADD COLUMN {name} {ddl}")


def relay_once(conn: sqlite3.Connection, db_name: str, publish=None) -> int:
    """Publish due outbox events. Returns how many were published.

    A failing event no longer blocks the ones behind it: it gets its own attempt count
    and a jittered retry time, and after OUTBOX_MAX_ATTEMPTS it is parked
    (published = -1) and counted in outbox_parked_total. A broker that is DOWN is not the
    event's fault: AMQPConnectionError stops the pass without blaming any event.
    """
    publish = publish or publish_event
    rows = conn.execute(
        "SELECT event_id, payload, attempts FROM outbox WHERE published=0 AND next_attempt_at <= ? "
        "ORDER BY created_at LIMIT 50", (time.time(),)).fetchall()
    sent = 0
    for row in rows:
        try:
            publish(json.loads(row["payload"]))
        except pika.exceptions.AMQPConnectionError:
            raise                                          # whole broker unavailable: back off
        except Exception as exc:                           # this event's problem (unroutable, nacked…)
            attempts = row["attempts"] + 1
            err = f"{type(exc).__name__}: {exc}"[:300]
            if attempts >= OUTBOX_MAX_ATTEMPTS:
                conn.execute("UPDATE outbox SET published=?, attempts=?, last_error=? WHERE event_id=?",
                             (PARKED, attempts, err, row["event_id"]))
                OUTBOX_PARKED.labels(db_name).inc()
                print(f"[outbox] PARKED event {row['event_id']} after {attempts} attempts: {err}", flush=True)
            else:
                conn.execute("UPDATE outbox SET attempts=?, last_error=?, next_attempt_at=? WHERE event_id=?",
                             (attempts, err, time.time() + retry.backoff(attempts, 1.0, 60.0), row["event_id"]))
                print(f"[outbox] event {row['event_id']} failed (attempt {attempts}/{OUTBOX_MAX_ATTEMPTS}): {err}",
                      flush=True)
            continue
        conn.execute("UPDATE outbox SET published=1 WHERE event_id=?", (row["event_id"],))
        sent += 1
    return sent


def relay_forever(db_path: str, interval: float = 0.5) -> None:
    """At-least-once relay: publish first, mark published second.

    A crash between the two re-publishes the event; consumers dedupe on event_id.
    While the broker is down the relay backs off with jitter (0.5 s up to 30 s).
    """
    db_name = os.path.basename(db_path)
    conn = connect(db_path)
    migrate_outbox(conn)
    conn.close()
    failures = 0
    while True:
        conn = None
        try:
            conn = connect(db_path)
            relay_once(conn, db_name)
            failures = 0
            delay = interval
        except Exception as exc:
            failures += 1
            delay = retry.backoff(failures, interval, 30.0)
            print(f"[outbox] relay error ({failures} in a row): {exc}; next pass in {delay:.1f}s", flush=True)
        finally:
            if conn:
                conn.close()
        time.sleep(delay)


def start_daemon(target, *args, name: str | None = None) -> threading.Thread:
    thread = threading.Thread(target=target, args=args, name=name, daemon=True)
    thread.start()
    return thread
