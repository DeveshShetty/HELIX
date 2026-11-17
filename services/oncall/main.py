"""On-Call & Escalation Service.

- Roster: the escalation chain [primary, secondary, manager]. STRONGLY
  CONSISTENT: one SQLite row, every write is a serialised transaction with an
  optimistic-concurrency check (PUT must send the version it read; stale -> 409),
  and pages read the chain inside the same transaction that creates them, so
  nobody is ever paged from a half-updated roster.
- Consumes incident.opened / .reopened -> page level 0 (simulated: logged + recorded).
- Ack timers live in the DATABASE (pages.deadline), not in memory: a restart
  mid-timer resumes the escalation instead of silently dropping it.
  Unacked past the deadline -> TIMED_OUT, page the next level (last level repeats).
- Consumes incident.acked / .resolved -> cancel pending pages.
- POST /pages/{id}/ack is idempotent and acks the ticket in the Ticket Service
  (deadline + retries) before recording the ack locally.
- Console UI at / for responders: ack, resolve (staff-written summary +
  resolution), reopen.
"""
import json
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from prometheus_client import Counter, Gauge
from pydantic import BaseModel, Field

from common.db import OUTBOX_DDL, already_processed, connect, mark_processed, start_daemon
from common.messaging import consume_forever
from common.observability import EVENTS_CONSUMED, instrument

SERVICE = "oncall"
DB_PATH = os.getenv("DB_PATH", "/data/oncall.db")
TICKETS_URL = os.getenv("TICKETS_URL", "http://localhost:8002")
CHAT_URL = os.getenv("CHAT_URL", "http://localhost:8001")
ACK_TIMEOUT_SECONDS = float(os.getenv("ACK_TIMEOUT_SECONDS", "30"))
DEFAULT_CHAIN = json.loads(os.getenv("DEFAULT_CHAIN", '["alice", "bob", "carol"]'))   # synthetic names

DDL = """
CREATE TABLE IF NOT EXISTS roster (
  id         INTEGER PRIMARY KEY CHECK (id = 1),
  version    INTEGER NOT NULL,
  chain      TEXT NOT NULL,
  updated_by TEXT,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pages (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
  page_id      TEXT UNIQUE,
  ticket_id    TEXT NOT NULL,
  responder_id TEXT NOT NULL,
  level        INTEGER NOT NULL,
  status       TEXT NOT NULL,          -- PENDING | ACKED | TIMED_OUT | CANCELLED
  sent_at      REAL NOT NULL,
  deadline     REAL NOT NULL,
  acked_at     REAL
);
CREATE INDEX IF NOT EXISTS pages_pending ON pages(status, deadline);
CREATE TABLE IF NOT EXISTS incidents (   -- local projection of ticket state (eventually consistent)
  ticket_id  TEXT PRIMARY KEY,
  title      TEXT,
  severity   TEXT,
  status     TEXT NOT NULL,
  revision   INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL
);
"""

PAGES_SENT = Counter("pages_sent_total", "Pages sent", ["level"])
ESCALATIONS = Counter("escalations_total", "Pages that timed out and escalated", ["from_level"])
PAGES_PENDING = Gauge("pages_pending", "Pages waiting for an ack")
ACKS = Counter("page_acks_total", "Page acks", ["outcome"])

app = FastAPI(title="On-Call & Escalation Service")
instrument(app, SERVICE)


def init_db() -> None:
    conn = connect(DB_PATH)
    conn.executescript(DDL + OUTBOX_DDL)
    conn.execute("INSERT OR IGNORE INTO roster(id, version, chain, updated_by, updated_at) VALUES (1, 1, ?, 'seed', ?)",
                 (json.dumps(DEFAULT_CHAIN), time.time()))
    conn.close()


def _page(conn, ticket_id: str, level: int) -> str:
    """Create a page. MUST be called inside a transaction (reads the roster consistently)."""
    chain = json.loads(conn.execute("SELECT chain FROM roster WHERE id=1").fetchone()["chain"])
    level = min(level, len(chain) - 1)
    now = time.time()
    cur = conn.execute(
        "INSERT INTO pages(ticket_id, responder_id, level, status, sent_at, deadline) VALUES (?,?,?,'PENDING',?,?)",
        (ticket_id, chain[level], level, now, now + ACK_TIMEOUT_SECONDS))
    page_id = f"P-{cur.lastrowid}"
    conn.execute("UPDATE pages SET page_id=? WHERE seq=?", (page_id, cur.lastrowid))
    PAGES_SENT.labels(str(level)).inc()
    print(f"[page] PAGING {chain[level]} for {ticket_id} (level {level}, ack within {ACK_TIMEOUT_SECONDS:.0f}s) {page_id}",
          flush=True)
    return page_id


def _cancel_pending(conn, ticket_id: str) -> int:
    return conn.execute("UPDATE pages SET status='CANCELLED' WHERE ticket_id=? AND status='PENDING'",
                        (ticket_id,)).rowcount


# ------------------------------------------------------------------ event consumer

def handle_incident_event(event: dict) -> None:
    etype = event["event_type"]
    data = event.get("data", {})
    ticket_id = data.get("ticket_id") or event.get("aggregate_id")
    revision = int(data.get("revision", 0))
    conn = connect(DB_PATH)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if already_processed(conn, event["event_id"]):
            conn.execute("ROLLBACK")
            EVENTS_CONSUMED.labels(SERVICE, etype, "duplicate").inc()
            print(f"[oncall] duplicate {etype} for {ticket_id} ignored", flush=True)
            return
        status = {"incident.opened": "OPEN", "incident.reopened": "OPEN",
                  "incident.acked": "ACKED", "incident.resolved": "RESOLVED"}[etype]
        conn.execute(
            """INSERT INTO incidents(ticket_id, title, severity, status, revision, updated_at) VALUES (?,?,?,?,?,?)
               ON CONFLICT(ticket_id) DO UPDATE SET status=excluded.status, revision=excluded.revision,
                 title=COALESCE(excluded.title, incidents.title),
                 severity=COALESCE(excluded.severity, incidents.severity), updated_at=excluded.updated_at
               WHERE excluded.revision >= incidents.revision""",
            (ticket_id, data.get("title"), data.get("severity"), status, revision, time.time()))
        if etype in ("incident.opened", "incident.reopened"):
            has_pending = conn.execute("SELECT 1 FROM pages WHERE ticket_id=? AND status='PENDING'",
                                       (ticket_id,)).fetchone()
            if not has_pending:
                _page(conn, ticket_id, 0)
        else:
            n = _cancel_pending(conn, ticket_id)
            if n:
                print(f"[oncall] {ticket_id} {status}: cancelled {n} pending page(s)", flush=True)
        mark_processed(conn, event["event_id"])
        conn.execute("COMMIT")
        EVENTS_CONSUMED.labels(SERVICE, etype, "processed").inc()
    except Exception:
        conn.execute("ROLLBACK")
        EVENTS_CONSUMED.labels(SERVICE, etype, "error").inc()
        raise
    finally:
        conn.close()


# ------------------------------------------------------------------ escalation timer

def escalation_loop() -> None:
    """Every second: escalate pages whose deadline passed. State is in the DB, so restarts resume."""
    while True:
        try:
            conn = connect(DB_PATH)
            conn.execute("BEGIN IMMEDIATE")
            overdue = conn.execute("SELECT * FROM pages WHERE status='PENDING' AND deadline <= ?",
                                   (time.time(),)).fetchall()
            for p in overdue:
                conn.execute("UPDATE pages SET status='TIMED_OUT' WHERE page_id=?", (p["page_id"],))
                ESCALATIONS.labels(str(p["level"])).inc()
                print(f"[escalate] {p['responder_id']} did not ack {p['ticket_id']} in time -> next level", flush=True)
                _page(conn, p["ticket_id"], p["level"] + 1)
            conn.execute("COMMIT")
            PAGES_PENDING.set(conn.execute("SELECT COUNT(*) FROM pages WHERE status='PENDING'").fetchone()[0])
            conn.close()
        except Exception as exc:
            print(f"[escalate] loop error: {exc!r}", flush=True)
        time.sleep(1)


# ------------------------------------------------------------------ calls to Tickets

def _call(base: str, name: str, method: str, path: str, body: dict | None = None,
          attempts: int = 3, timeout: float = 3.0) -> dict:
    """Timeout + retry with backoff. 4xx are not retried (they are answers, not failures)."""
    for i in range(attempts):
        try:
            r = httpx.request(method, f"{base}{path}", json=body, timeout=timeout)
            if r.status_code < 500:
                if r.status_code >= 400:
                    raise HTTPException(r.status_code, r.json().get("detail", r.text))
                return r.json()
        except httpx.TransportError as exc:
            if i == attempts - 1:
                raise HTTPException(503, f"{name} unavailable: {exc!r}")
        time.sleep(0.3 * 2 ** i)
    raise HTTPException(503, f"{name} unavailable")


def tickets_call(method: str, path: str, body: dict | None = None, attempts: int = 3) -> dict:
    return _call(TICKETS_URL, "Ticket Service", method, path, body, attempts)


def chat_call(method: str, path: str, body: dict | None = None, attempts: int = 2, timeout: float = 3.0) -> dict:
    # Conversations (transcripts, live messages) are owned by the Chat service.
    return _call(CHAT_URL, "Chat Service", method, path, body, attempts, timeout)


# ------------------------------------------------------------------ startup

init_db()
start_daemon(consume_forever, "oncall.incident-lifecycle",
             ["incident.opened", "incident.acked", "incident.resolved", "incident.reopened"],
             handle_incident_event, name="oncall-consumer")
start_daemon(escalation_loop, name="oncall-escalation-loop")


# ------------------------------------------------------------------ API

class RosterIn(BaseModel):
    version: int
    chain: list[str] = Field(min_length=1, max_length=10)
    updated_by: str = "admin"


class AckIn(BaseModel):
    responder_id: str = Field(min_length=1)


class ResolveIn(BaseModel):
    responder_id: str = Field(min_length=1)
    summary: str = Field(min_length=5, max_length=200)
    resolution: str = Field(min_length=5)
    add_to_knowledge_base: bool = True


class ReopenIn(BaseModel):
    responder_id: str = Field(min_length=1)
    reason: str = Field(min_length=3)


@app.get("/roster")
def get_roster():
    conn = connect(DB_PATH)
    try:
        r = dict(conn.execute("SELECT version, chain, updated_by, updated_at FROM roster WHERE id=1").fetchone())
    finally:
        conn.close()
    r["chain"] = json.loads(r["chain"])
    return r


@app.put("/roster")
def put_roster(body: RosterIn):
    conn = connect(DB_PATH)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT version FROM roster WHERE id=1").fetchone()["version"]
        if body.version != current:
            conn.execute("ROLLBACK")
            raise HTTPException(409, f"roster changed since you read it (you sent version {body.version}, "
                                     f"current is {current}); re-read and retry")
        conn.execute("UPDATE roster SET version=?, chain=?, updated_by=?, updated_at=? WHERE id=1",
                     (current + 1, json.dumps(body.chain), body.updated_by, time.time()))
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get_roster()


@app.get("/pages")
def list_pages(ticket_id: str | None = None, limit: int = 100):
    conn = connect(DB_PATH)
    try:
        q = "SELECT page_id, ticket_id, responder_id, level, status, sent_at, deadline, acked_at FROM pages"
        rows = (conn.execute(q + " WHERE ticket_id=? ORDER BY seq DESC", (ticket_id,)) if ticket_id
                else conn.execute(q + " ORDER BY seq DESC LIMIT ?", (limit,))).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


@app.post("/pages/{page_id}/ack")
def ack_page(page_id: str, body: AckIn):
    conn = connect(DB_PATH)
    try:
        page = conn.execute("SELECT * FROM pages WHERE page_id=?", (page_id,)).fetchone()
    finally:
        conn.close()
    if not page:
        raise HTTPException(404, "page not found")
    if page["status"] == "ACKED":
        ACKS.labels("duplicate").inc()
        return dict(page)                                    # idempotent
    # Ticket Service first (idempotent there too); only then record locally.
    tickets_call("POST", f"/tickets/{page['ticket_id']}/ack", {"responder_id": body.responder_id})
    conn = connect(DB_PATH)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE pages SET status='ACKED', acked_at=? WHERE page_id=?", (time.time(), page_id))
        _cancel_pending(conn, page["ticket_id"])
        conn.execute("COMMIT")
        row = conn.execute("SELECT * FROM pages WHERE page_id=?", (page_id,)).fetchone()
    finally:
        conn.close()
    ACKS.labels("acked").inc()
    print(f"[oncall] {body.responder_id} acked {page['ticket_id']} via {page_id}", flush=True)
    return dict(row)


@app.post("/incidents/{ticket_id}/resolve")
def resolve_incident(ticket_id: str, body: ResolveIn):
    return tickets_call("POST", f"/tickets/{ticket_id}/resolve", body.model_dump())


@app.post("/incidents/{ticket_id}/reopen")
def reopen_incident(ticket_id: str, body: ReopenIn):
    return tickets_call("POST", f"/tickets/{ticket_id}/reopen", body.model_dump())


class AgentMessageIn(BaseModel):
    responder_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=4000)


class CloseChatIn(BaseModel):
    responder_id: str = Field(min_length=1)


@app.get("/tickets/{ticket_id}/conversation")
def ticket_conversation(ticket_id: str, after: int = 0):
    """The customer's stored conversation for this ticket: bot exchange (with why it got stuck) + live chat."""
    return chat_call("GET", f"/conversations/by-ticket/{ticket_id}?after={after}")


@app.post("/conversations/{cid}/messages")
def agent_message(cid: str, body: AgentMessageIn):
    return chat_call("POST", f"/conversations/{cid}/messages",
                     {"role": "agent", "author": body.responder_id, "text": body.text})


@app.post("/conversations/{cid}/close")
def close_chat(cid: str, body: CloseChatIn):
    """End the live chat; returns a DRAFT summary the responder must review before resolving."""
    return chat_call("POST", f"/conversations/{cid}/close", {"agent_id": body.responder_id},
                     attempts=1, timeout=60.0)   # summary drafting may call the LLM


@app.get("/console/data")
def console_data():
    """Everything the console shows. Live tickets from the Ticket Service, falling back
    to this service's own (eventually consistent) projection if Tickets is down."""
    try:
        tickets, source = tickets_call("GET", "/tickets?limit=30", attempts=1), "live"
    except HTTPException:
        conn = connect(DB_PATH)
        try:
            tickets = [dict(r) for r in conn.execute("SELECT * FROM incidents ORDER BY updated_at DESC LIMIT 30")]
        finally:
            conn.close()
        source = "local projection (Ticket Service unreachable)"
    pages = list_pages(limit=200)
    by_ticket: dict[str, list] = {}
    for p in pages:
        by_ticket.setdefault(p["ticket_id"], []).append(p)
    try:    # which tickets have a customer waiting in / talking over live chat
        live = {c["ticket_id"]: c for c in chat_call("GET", "/conversations?status=WAITING_AGENT,LIVE", attempts=1)}
    except HTTPException:
        live = {}
    for t in tickets:
        t["pages"] = by_ticket.get(t["ticket_id"], [])
        c = live.get(t["ticket_id"])
        t["live_chat"] = {"conversation_id": c["conversation_id"], "status": c["status"],
                          "agent_id": c["agent_id"]} if c else None
    return {"now": time.time(), "source": source, "roster": get_roster(),
            "ack_timeout_seconds": ACK_TIMEOUT_SECONDS, "tickets": tickets}


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE}


@app.get("/", include_in_schema=False)
def console():
    return FileResponse(Path(__file__).parent / "static" / "console.html")
