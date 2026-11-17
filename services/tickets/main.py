"""Incident / Ticket Service: the system of record for incidents.

State machine:  OPEN -> ACKED -> RESOLVED   (also OPEN -> RESOLVED)

Consistency pattern: TRANSACTIONAL OUTBOX.
Every state change and the event announcing it are written in ONE SQLite
transaction (tickets row + outbox row). A background relay publishes outbox
rows to RabbitMQ and marks them published. So:
  - a ticket can never change state without its event eventually being sent;
  - an event can never be sent for a change that was rolled back;
  - if RabbitMQ is down, tickets are still accepted and the outbox backlog
    grows, then drains when the broker returns (see outbox_backlog metric).
Delivery is at-least-once; consumers dedupe on event_id.

Idempotency:
  - POST /tickets requires an Idempotency-Key header; the same key always
    returns the same ticket, so a client retry never creates a duplicate.
  - Repeating a transition the ticket is already in returns 200 unchanged
    and emits NO new event.
"""
import json
import os
import time

from fastapi import FastAPI, Header, HTTPException, Response
from prometheus_client import Counter, Gauge
from pydantic import BaseModel, Field

from common.db import OUTBOX_DDL, connect, enqueue, outbox_backlog, relay_forever, start_daemon
from common.messaging import new_event
from common.observability import instrument

SERVICE = "tickets"
DB_PATH = os.getenv("DB_PATH", "/data/tickets.db")

TICKETS_DDL = """
CREATE TABLE IF NOT EXISTS tickets (
  seq               INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id         TEXT UNIQUE,
  status            TEXT NOT NULL CHECK (status IN ('OPEN','ACKED','RESOLVED')),
  title             TEXT NOT NULL,
  question          TEXT NOT NULL,
  severity          TEXT NOT NULL,
  conversation_id   TEXT,
  confidence        REAL,
  retrieved_doc_ids TEXT NOT NULL,
  acked_by          TEXT,
  resolution        TEXT,
  resolved_by       TEXT,
  created_at        REAL NOT NULL,
  acked_at          REAL,
  resolved_at       REAL
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
  key        TEXT PRIMARY KEY,
  ticket_id  TEXT NOT NULL,
  created_at REAL NOT NULL
);
"""

TRANSITIONS = {"ACKED": {"OPEN"}, "RESOLVED": {"OPEN", "ACKED"}}   # target -> allowed sources

TICKETS_CREATED = Counter("tickets_created_total", "Tickets created", ["severity"])
TRANSITIONS_TOTAL = Counter("ticket_transitions_total", "Ticket state transitions", ["to", "outcome"])
OUTBOX_BACKLOG = Gauge("outbox_backlog", "Unpublished outbox events", ["service"])


def init_db() -> None:
    conn = connect(DB_PATH)
    conn.executescript(TICKETS_DDL + OUTBOX_DDL)
    conn.close()


def backlog_monitor() -> None:
    while True:
        try:
            OUTBOX_BACKLOG.labels(SERVICE).set(outbox_backlog(DB_PATH))
        except Exception as exc:
            print(f"[tickets] backlog monitor error: {exc}", flush=True)
        time.sleep(2)


init_db()
start_daemon(relay_forever, DB_PATH, name="tickets-outbox-relay")
start_daemon(backlog_monitor, name="tickets-backlog-monitor")

app = FastAPI(title="Incident / Ticket Service")
instrument(app, SERVICE)


class TicketIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    question: str = ""
    severity: str = Field("medium", pattern="^(low|medium|high)$")
    conversation_id: str | None = None
    confidence: float | None = None
    retrieved_doc_ids: list[str] = []


class AckIn(BaseModel):
    responder_id: str = Field(min_length=1)


class ResolveIn(BaseModel):
    responder_id: str = Field(min_length=1)
    resolution: str = Field(min_length=5)


def _row_to_ticket(row) -> dict:
    t = dict(row)
    t.pop("seq", None)
    t["retrieved_doc_ids"] = json.loads(t["retrieved_doc_ids"])
    return t


def _load(conn, ticket_id: str) -> dict:
    row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
    if not row:
        raise HTTPException(404, "ticket not found")
    return _row_to_ticket(row)


@app.post("/tickets", status_code=201)
def create_ticket(body: TicketIn, response: Response,
                  idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1)):
    now = time.time()
    conn = connect(DB_PATH)
    try:
        conn.execute("BEGIN IMMEDIATE")   # serialise writers: the key check and insert are atomic
        existing = conn.execute("SELECT ticket_id FROM idempotency_keys WHERE key=?",
                                (idempotency_key,)).fetchone()
        if existing:
            conn.execute("ROLLBACK")
            response.status_code = 200
            t = _load(conn, existing["ticket_id"])
            return {"ticket_id": t["ticket_id"], "status": t["status"], "created_at": t["created_at"]}

        cur = conn.execute(
            """INSERT INTO tickets(status, title, question, severity, conversation_id, confidence,
               retrieved_doc_ids, created_at) VALUES ('OPEN',?,?,?,?,?,?,?)""",
            (body.title, body.question, body.severity, body.conversation_id, body.confidence,
             json.dumps(body.retrieved_doc_ids), now))
        ticket_id = f"T-{cur.lastrowid:06d}"
        conn.execute("UPDATE tickets SET ticket_id=? WHERE seq=?", (ticket_id, cur.lastrowid))
        conn.execute("INSERT INTO idempotency_keys(key, ticket_id, created_at) VALUES (?,?,?)",
                     (idempotency_key, ticket_id, now))
        enqueue(conn, new_event("incident.opened", ticket_id, {
            "ticket_id": ticket_id, "title": body.title, "question": body.question,
            "severity": body.severity, "created_at": now}))
        conn.execute("COMMIT")
    except HTTPException:
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    TICKETS_CREATED.labels(body.severity).inc()
    print(f"[tickets] {ticket_id} OPEN (incident.opened queued in outbox)", flush=True)
    return {"ticket_id": ticket_id, "status": "OPEN", "created_at": now}


@app.get("/tickets")
def list_tickets(status: str | None = None, limit: int = 100):
    conn = connect(DB_PATH)
    try:
        if status:
            rows = conn.execute("SELECT * FROM tickets WHERE status=? ORDER BY seq DESC LIMIT ?",
                                (status.upper(), limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tickets ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return [_row_to_ticket(r) for r in rows]


@app.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str):
    conn = connect(DB_PATH)
    try:
        return _load(conn, ticket_id)
    finally:
        conn.close()


def _transition(ticket_id: str, target: str, updates: dict, event_type: str, event_data: dict) -> dict:
    """Apply a state transition + enqueue its event atomically. Idempotent on repeat."""
    conn = connect(DB_PATH)
    try:
        conn.execute("BEGIN IMMEDIATE")
        t = _load(conn, ticket_id)
        if t["status"] == target:
            conn.execute("ROLLBACK")
            TRANSITIONS_TOTAL.labels(target, "noop").inc()
            return t                                   # repeat: 200, unchanged, no event
        if t["status"] not in TRANSITIONS[target]:
            conn.execute("ROLLBACK")
            TRANSITIONS_TOTAL.labels(target, "rejected").inc()
            raise HTTPException(409, f"cannot move ticket from {t['status']} to {target}")
        cols = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE tickets SET status=?, {cols} WHERE ticket_id=?",
                     (target, *updates.values(), ticket_id))
        enqueue(conn, new_event(event_type, ticket_id, {"ticket_id": ticket_id, **event_data}))
        conn.execute("COMMIT")
        TRANSITIONS_TOTAL.labels(target, "applied").inc()
        print(f"[tickets] {ticket_id} {t['status']} -> {target} ({event_type} queued)", flush=True)
        return _load(conn, ticket_id)
    except HTTPException:
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@app.post("/tickets/{ticket_id}/ack")
def ack(ticket_id: str, body: AckIn):
    now = time.time()
    return _transition(ticket_id, "ACKED", {"acked_by": body.responder_id, "acked_at": now},
                       "incident.acked", {"responder_id": body.responder_id, "acked_at": now})


@app.post("/tickets/{ticket_id}/resolve")
def resolve(ticket_id: str, body: ResolveIn):
    now = time.time()
    t = get_ticket(ticket_id)
    return _transition(ticket_id, "RESOLVED",
                       {"resolution": body.resolution, "resolved_by": body.responder_id, "resolved_at": now},
                       "incident.resolved",
                       {"title": t["title"], "question": t["question"], "resolution": body.resolution,
                        "resolved_by": body.responder_id, "resolved_at": now})


@app.get("/outbox")
def outbox_status():
    """Debug view for the outbox demo: how many events are waiting for the broker."""
    conn = connect(DB_PATH)
    try:
        pending = conn.execute("SELECT COUNT(*) FROM outbox WHERE published=0").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    finally:
        conn.close()
    return {"pending": pending, "published": total - pending}


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE}
