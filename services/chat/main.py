"""Chat / RAG Support Service

Conversational front door. Retrieves context from the Knowledge Base (behind a
circuit breaker + timeout + retry), drafts a grounded answer, and decides
answer-vs-escalate using a tunable confidence threshold. Escalation opens a
ticket in the Incident/Ticket Service. Exports token-cost metrics.
Implemented in Phase 2.
"""
import os

from fastapi import FastAPI

from common.observability import instrument

SERVICE = "chat"
DB_PATH = os.getenv("DB_PATH", "/data/chat.db")

app = FastAPI(title="Chat / RAG Support Service")
instrument(app, SERVICE)


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE}
