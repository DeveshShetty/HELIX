"""Incident / Ticket Service

System of record for incidents: OPEN -> ACKED -> RESOLVED.
Every state change writes an event to the transactional outbox in the same
local transaction (incident.opened / incident.acked / incident.resolved).
Implemented in Phase 1.
"""
import os

from fastapi import FastAPI

from common.observability import instrument

SERVICE = "tickets"
DB_PATH = os.getenv("DB_PATH", "/data/tickets.db")

app = FastAPI(title="Incident / Ticket Service")
instrument(app, SERVICE)


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE}
