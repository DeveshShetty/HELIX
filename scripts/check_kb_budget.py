"""Check that Knowledge skips PAID embeddings once the shared daily AI budget is used up.

nomic-embed-text needs Ollama with that model pulled, so this uses a stand-in: the
local embedder, renamed so it is priced and metered like nomic-embed-text. It runs
the real embed_texts / index_document code against a throw-away database, with the
broker address set to nothing so this process never takes events from the live queue.

    docker compose exec -T knowledge sh -c "DB_PATH=/tmp/kbcheck.db RABBIT_HOST=nowhere python -" < scripts/check_kb_budget.py
"""
import os

assert os.environ.get("DB_PATH", "").startswith("/tmp/"), "run with DB_PATH=/tmp/... (never the live DB)"

from common import budget                       # noqa: E402
from services.knowledge import main as kb       # noqa: E402  (loads the seed into /tmp in the background)
import time                                      # noqa: E402

while not kb._startup["ready"]:                  # startup is asynchronous since audit fix 5
    time.sleep(0.2)


class PaidStandIn:
    """Behaves like the local embedder, but is priced and metered as nomic-embed-text."""
    name = "nomic-embed-text"

    def __init__(self, inner):
        self.inner = inner

    def embed(self, texts):
        return self.inner.embed(texts)


def use_stand_in():
    with kb._lock:                               # refresh_cache() puts the real embedder back
        if not isinstance(kb._cache["embedder"], PaidStandIn):
            kb._cache["embedder"] = PaidStandIn(kb._cache["embedder"])


use_stand_in()

print("budget today before:", budget.today(), "ceiling tokens:", budget.DAILY_TOKEN_CEILING)
doc = {"doc_id": "budget-check-1", "title": "Change the invoice currency", "source_type": "manual",
       "tags": "billing", "text": "Open Billing > Settings, set the billing country, then reload invoices."}
print("index_document:", kb.index_document(doc, "api"))
use_stand_in()
try:
    kb.embed_texts(kb._cache["embedder"], ["how do I change invoice currency"], "query")
    print("query embedding: allowed")
except kb.BudgetExhausted as exc:
    print("query embedding: skipped ->", exc)
conn = kb.connect(os.environ["DB_PATH"])
print("document stored:", conn.execute("SELECT COUNT(*) FROM documents WHERE doc_id='budget-check-1'").fetchone()[0],
      "| chunks indexed:", conn.execute("SELECT COUNT(*) FROM chunks WHERE doc_id='budget-check-1'").fetchone()[0])
from prometheus_client import REGISTRY           # noqa: E402
for m in REGISTRY.collect():
    for s in m.samples:
        if s.name == "ai_budget_denied_total":
            print(f"ai_budget_denied_total{s.labels} = {s.value}")
