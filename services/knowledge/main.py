"""Knowledge Base / Ingestion Service

Owns the vector index (tagged with an embedding-model version). Exposes a
retrieval API, seeds runbooks/FAQs, and consumes incident.resolved to re-index
the resolution with an idempotent consumer. Implemented in Phase 1.
"""
import os

from fastapi import FastAPI

from common.observability import instrument

SERVICE = "knowledge"
DB_PATH = os.getenv("DB_PATH", "/data/knowledge.db")

app = FastAPI(title="Knowledge Base / Ingestion Service")
instrument(app, SERVICE)


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE}
