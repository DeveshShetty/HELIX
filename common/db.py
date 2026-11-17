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

from common.messaging import publish_event

OUTBOX_DDL = """
CREATE TABLE IF NOT EXISTS outbox (
  event_id   TEXT PRIMARY KEY,
  payload    TEXT NOT NULL,
  published  INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
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


def relay_forever(db_path: str, interval: float = 0.5) -> None:
    """At-least-once relay: publish first, mark published second.

    A crash between the two re-publishes the event; consumers dedupe on event_id.
    """
    while True:
        try:
            conn = connect(db_path)
            rows = conn.execute(
                "SELECT event_id, payload FROM outbox WHERE published=0 ORDER BY created_at LIMIT 50"
            ).fetchall()
            for row in rows:
                publish_event(json.loads(row["payload"]))
                conn.execute("UPDATE outbox SET published=1 WHERE event_id=?", (row["event_id"],))
            conn.close()
        except Exception as exc:
            print(f"[outbox] relay error: {exc}", flush=True)
        time.sleep(interval)


def start_daemon(target, *args, name: str | None = None) -> threading.Thread:
    thread = threading.Thread(target=target, args=args, name=name, daemon=True)
    thread.start()
    return thread
