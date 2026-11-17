"""On-Call & Escalation Service

Owns the on-call roster (strongly consistent), consumes incident.opened,
simulates paging, runs ack timers and walks the escalation chain. Ack handling
is idempotent. Implemented in Phase 3.
"""
import os

from fastapi import FastAPI

from common.observability import instrument

SERVICE = "oncall"
DB_PATH = os.getenv("DB_PATH", "/data/oncall.db")

app = FastAPI(title="On-Call & Escalation Service")
instrument(app, SERVICE)


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE}
