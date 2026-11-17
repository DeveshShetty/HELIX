"""Knowledge Base / Ingestion Service (owner: Liu Yang)

##############################################################################
# TEMPORARY MOCK - replace with the real implementation (docs/TASKS.md).
#
# This mock exists only so the Chat service can be built and demoed before
# the real Knowledge Base is ready. It implements the CONTRACT of
# POST /search and GET /index (docs/CONTRACTS.md section 1) with a simple
# in-memory TF-IDF cosine search over the seed documents. It has no
# persistence, no embeddings, no index versioning and no event consumer.
##############################################################################
"""
import math
import os
import re
import time
from collections import Counter
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

from common.observability import instrument

SERVICE = "knowledge"
SEED_DIR = Path(os.getenv("SEED_DIR", Path(__file__).parent / "seed"))
INDEX_VERSION = "mock-tfidf"

app = FastAPI(title="Knowledge Base / Ingestion Service (MOCK)")
instrument(app, SERVICE)

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def parse_seed(path: Path) -> dict | None:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        return None
    _, header, body = raw.split("---", 2)
    meta = {}
    for line in header.strip().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip('"')
    return {"doc_id": meta.get("id", path.stem), "title": meta.get("title", path.stem),
            "source_type": meta.get("type", "faq"), "tags": meta.get("tags", ""),
            "text": body.strip()}


DOCS = [d for p in sorted(SEED_DIR.glob("*.md")) if p.name != "README.md" and (d := parse_seed(p))]
BUILT_AT = time.time()


def _doc_terms(doc: dict) -> list[str]:
    # Title and tags count double: they are the strongest signal of what a doc is about.
    return tokenize(doc["title"]) * 2 + tokenize(doc["tags"]) * 2 + tokenize(doc["text"])


_DF = Counter(t for d in DOCS for t in set(_doc_terms(d)))
_IDF = {t: math.log((1 + len(DOCS)) / (1 + df)) + 1 for t, df in _DF.items()}


def _vector(terms: list[str]) -> dict[str, float]:
    tf = Counter(terms)
    vec = {t: c * _IDF.get(t, 0.0) for t, c in tf.items()}
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {t: v / norm for t, v in vec.items()}


_DOC_VECS = [_vector(_doc_terms(d)) for d in DOCS]


class SearchRequest(BaseModel):
    query: str
    top_k: int = 3


@app.post("/search")
def search(body: SearchRequest):
    q = _vector(tokenize(body.query))
    scored = []
    for doc, vec in zip(DOCS, _DOC_VECS):
        score = sum(w * vec.get(t, 0.0) for t, w in q.items())
        if score > 0:
            scored.append((score, doc))
    scored.sort(key=lambda x: x[0], reverse=True)
    return {
        "index_version": INDEX_VERSION,
        "embedding_model": "mock-tfidf",
        "results": [
            {"doc_id": d["doc_id"], "title": d["title"], "source_type": d["source_type"],
             "text": d["text"], "score": round(s, 4)}
            for s, d in scored[: body.top_k]
        ],
    }


@app.get("/index")
def index():
    return {"active_version": INDEX_VERSION, "embedding_model": "mock-tfidf",
            "documents": len(DOCS), "chunks": len(DOCS), "built_at": BUILT_AT}


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE, "mock": True, "documents": len(DOCS)}
