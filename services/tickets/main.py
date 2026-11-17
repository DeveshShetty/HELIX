"""Incident / Ticket Service (owner: Aditya Mahendru)

##############################################################################
# TEMPORARY MOCK - replace with the real implementation (docs/TASKS.md).
#
# This mock exists only so the Chat service can escalate before the real
# Ticket Service is ready. It implements the CONTRACT of POST /tickets
# (with Idempotency-Key), GET /tickets/{id}, GET /tickets, /ack and /resolve
# (docs/CONTRACTS.md section 2) IN MEMORY. It has no database, no outbox and
# publishes NO events, so On-Call and Knowledge never hear about tickets.
##############################################################################
"""
import itertools
import threading
import time

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel

from common.observability import instrument

SERVICE = "tickets"

app = FastAPI(title="Incident / Ticket Service (MOCK)")
instrument(app, SERVICE)

_lock = threading.Lock()
_tickets: dict[str, dict] = {}
_by_key: dict[str, str] = {}
_seq = itertools.count(1)


class TicketIn(BaseModel):
    title: str
    question: str = ""
    severity: str = "medium"
    conversation_id: str | None = None
    confidence: float | None = None
    retrieved_doc_ids: list[str] = []


class AckIn(BaseModel):
    responder_id: str


class ResolveIn(BaseModel):
    responder_id: str
    resolution: str


@app.post("/tickets", status_code=201)
def create_ticket(body: TicketIn, response: Response,
                  idempotency_key: str = Header(..., alias="Idempotency-Key")):
    with _lock:
        if idempotency_key in _by_key:
            response.status_code = 200
            t = _tickets[_by_key[idempotency_key]]
        else:
            tid = f"T-{next(_seq):06d}"
            t = {"ticket_id": tid, "status": "OPEN", **body.model_dump(),
                 "acked_by": None, "resolution": None, "resolved_by": None,
                 "created_at": time.time(), "acked_at": None, "resolved_at": None}
            _tickets[tid] = t
            _by_key[idempotency_key] = tid
    return {"ticket_id": t["ticket_id"], "status": t["status"], "created_at": t["created_at"]}


@app.get("/tickets")
def list_tickets(status: str | None = None):
    return [t for t in _tickets.values() if status is None or t["status"] == status]


@app.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str):
    if ticket_id not in _tickets:
        raise HTTPException(404, "ticket not found")
    return _tickets[ticket_id]


@app.post("/tickets/{ticket_id}/ack")
def ack(ticket_id: str, body: AckIn):
    t = get_ticket(ticket_id)
    if t["status"] == "OPEN":
        t.update(status="ACKED", acked_by=body.responder_id, acked_at=time.time())
    elif t["status"] == "RESOLVED":
        raise HTTPException(409, "ticket already resolved")
    return t


@app.post("/tickets/{ticket_id}/resolve")
def resolve(ticket_id: str, body: ResolveIn):
    t = get_ticket(ticket_id)
    if t["status"] != "RESOLVED":
        t.update(status="RESOLVED", resolution=body.resolution,
                 resolved_by=body.responder_id, resolved_at=time.time())
    return t


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE, "mock": True}
