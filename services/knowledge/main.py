"""Knowledge Base / Ingestion Service: owns the vector index.

- Loads Helix Cloud runbooks / FAQs / past incidents from seed/ on startup.
- POST /search: embed the query with the ACTIVE index's model, return the best
  chunk per document by cosine similarity.
- Consumes incident.resolved: indexes the STAFF-WRITTEN summary + resolution
  (never the user's original text: re-indexed text reaches every future
  user's prompt, so user text here would be a stored prompt-injection vector).
  IDEMPOTENT: dedupe on event_id, upsert on doc_id, and keyed on the ticket's
  revision so an older event can never overwrite a newer one.
- Consumes incident.reopened: WITHDRAWS the indexed resolution (it was wrong).
- Every chunk is stamped with the embedding model that produced it and its
  source (origin + author), and both are returned with search results.
- POST /index/rebuild: builds a NEW index version (optionally with a different
  embedding model) alongside the active one. Searches keep using the old index
  until the new one is complete; then the switch is one atomic update.
  Documents that arrive during the build are caught up before the switch.
- POST /admin/fault: inject latency/errors into /search for resilience demos.
"""
import json
import os
import random
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from prometheus_client import Counter, Gauge
from pydantic import BaseModel, Field

from common.db import OUTBOX_DDL, already_processed, connect, mark_processed, start_daemon
from common.messaging import consume_forever
from common.observability import EVENTS_CONSUMED, instrument
from services.knowledge import embedders

SERVICE = "knowledge"
DB_PATH = os.getenv("DB_PATH", "/data/knowledge.db")
SEED_DIR = Path(os.getenv("SEED_DIR", Path(__file__).parent / "seed"))
DEFAULT_MODEL = os.getenv("EMBEDDING_MODEL", "hash-v1")
CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "1200"))

DDL = """
CREATE TABLE IF NOT EXISTS documents (
  doc_id      TEXT PRIMARY KEY,
  title       TEXT NOT NULL,
  source_type TEXT NOT NULL,
  tags        TEXT NOT NULL DEFAULT '',
  text        TEXT NOT NULL,
  origin      TEXT NOT NULL,            -- seed | api | incident
  author      TEXT NOT NULL DEFAULT 'kb-editors',
  updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS incident_revisions (   -- latest revision seen per ticket
  ticket_id   TEXT PRIMARY KEY,
  revision    INTEGER NOT NULL,
  state       TEXT NOT NULL,            -- INDEXED | WITHDRAWN
  updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS indexes (
  version     TEXT PRIMARY KEY,
  model       TEXT NOT NULL,
  status      TEXT NOT NULL,            -- BUILDING | ACTIVE | RETIRED | FAILED
  state_json  TEXT NOT NULL DEFAULT '{}',
  started_at  REAL NOT NULL,
  built_at    REAL,
  chunks      INTEGER NOT NULL DEFAULT 0,
  error       TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
  version     TEXT NOT NULL,
  chunk_id    TEXT NOT NULL,
  doc_id      TEXT NOT NULL,
  model       TEXT NOT NULL,            -- embedding model that produced vector_json
  text        TEXT NOT NULL,
  vector_json TEXT NOT NULL,
  PRIMARY KEY (version, chunk_id)
);
CREATE INDEX IF NOT EXISTS chunks_by_doc ON chunks(version, doc_id);
"""

KB_CHUNKS = Gauge("kb_chunks", "Chunks in an index", ["index_version"])
REINDEX = Counter("kb_reindex_total", "Documents (re)indexed", ["origin", "outcome"])
REBUILDS = Counter("kb_index_builds_total", "Full index builds", ["model", "outcome"])
EMBED_TOKENS = Counter("embedding_tokens_total", "Tokens sent to the embedding model", ["model", "purpose"])
EMBED_COST = Counter("embedding_cost_usd_total", "Notional embedding cost in USD", ["model", "purpose"])
# Notional price, as if nomic-embed-text were a hosted embedding API. hash-v1 is free (local arithmetic).
EMBED_PRICE_PER_1K = {"hash-v1": 0.0, "nomic-embed-text": float(os.getenv("PRICE_EMBED_PER_1K", "0.00002"))}

app = FastAPI(title="Knowledge Base / Ingestion Service")
instrument(app, SERVICE)

_lock = threading.RLock()        # guards the in-memory cache of the active index
_cache: dict = {"version": None, "model": None, "embedder": None, "chunks": []}
_fault = {"latency_ms": 0, "error_rate": 0.0}


# ---------------------------------------------------------------- helpers

def parse_seed(path: Path) -> dict | None:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        return None
    _, header, body = raw.split("---", 2)
    meta = {}
    for line in header.strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"')
    return {"doc_id": meta.get("id", path.stem), "title": meta.get("title", path.stem),
            "source_type": meta.get("type", "faq"), "tags": meta.get("tags", ""), "text": body.strip()}


def chunk_document(doc: dict) -> list[tuple[str, str]]:
    """Split on paragraphs into chunks of at most CHUNK_CHARS. Returns [(chunk_id, text)]."""
    paras = [p.strip() for p in doc["text"].split("\n\n") if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) + 2 > CHUNK_CHARS:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return [(f"{doc['doc_id']}#{i}", c) for i, c in enumerate(chunks or [doc["text"]])]


def embed_input(doc: dict, chunk_text: str) -> str:
    # Title and tags are embedded with every chunk: they say what the chunk is about.
    return f"{doc['title']}\n{doc['tags']}\n{chunk_text}"


def embed_texts(embedder, texts: list[str], purpose: str) -> list:
    """All embedding calls go through here so their (notional) cost is counted."""
    tokens = sum(max(1, len(t) // 4) for t in texts)
    EMBED_TOKENS.labels(embedder.name, purpose).inc(tokens)
    EMBED_COST.labels(embedder.name, purpose).inc(tokens / 1000 * EMBED_PRICE_PER_1K.get(embedder.name, 0.0))
    return embedder.embed(texts)


def embed_document(embedder, doc: dict, purpose: str = "index") -> list[tuple[str, str, list | dict]]:
    pieces = chunk_document(doc)
    vectors = embed_texts(embedder, [embed_input(doc, t) for _, t in pieces], purpose)
    return [(cid, text, vec) for (cid, text), vec in zip(pieces, vectors)]


def write_chunks(conn, version: str, model: str, doc_id: str, rows) -> None:
    conn.execute("DELETE FROM chunks WHERE version=? AND doc_id=?", (version, doc_id))
    conn.executemany(
        "INSERT INTO chunks(version, chunk_id, doc_id, model, text, vector_json) VALUES (?,?,?,?,?,?)",
        [(version, cid, doc_id, model, text, json.dumps(vec)) for cid, text, vec in rows])


def upsert_document_row(conn, doc: dict, origin: str) -> None:
    conn.execute(
        """INSERT INTO documents(doc_id, title, source_type, tags, text, origin, author, updated_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(doc_id) DO UPDATE SET title=excluded.title, source_type=excluded.source_type,
             tags=excluded.tags, text=excluded.text, origin=excluded.origin, author=excluded.author,
             updated_at=excluded.updated_at""",
        (doc["doc_id"], doc["title"], doc["source_type"], doc.get("tags", ""), doc["text"], origin,
         doc.get("author", "kb-editors"), time.time()))


def active_index(conn) -> dict | None:
    row = conn.execute("SELECT * FROM indexes WHERE status='ACTIVE'").fetchone()
    return dict(row) if row else None


def refresh_cache() -> None:
    """Load the active index into memory (search is a brute-force scan; fine at this size)."""
    conn = connect(DB_PATH)
    try:
        idx = active_index(conn)
        if not idx:
            return
        rows = conn.execute(
            """SELECT c.chunk_id, c.doc_id, c.model, c.text, c.vector_json, d.title, d.source_type,
                      d.origin, d.author
               FROM chunks c JOIN documents d ON d.doc_id = c.doc_id WHERE c.version=?""",
            (idx["version"],)).fetchall()
    finally:
        conn.close()
    with _lock:
        _cache.update(version=idx["version"], model=idx["model"],
                      embedder=embedders.load(idx["model"], json.loads(idx["state_json"])),
                      chunks=[{**dict(r), "vector": json.loads(r["vector_json"])} for r in rows])
    KB_CHUNKS.labels(idx["version"]).set(len(rows))


# ---------------------------------------------------------------- index builds

def build_index(model: str) -> str:
    """Build a complete new index version, then atomically make it ACTIVE."""
    started = time.time()
    version = f"{model}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(started))}"
    conn = connect(DB_PATH)
    conn.execute("INSERT INTO indexes(version, model, status, started_at) VALUES (?,?,'BUILDING',?)",
                 (version, model, started))
    conn.close()
    print(f"[index] building {version}", flush=True)
    try:
        conn = connect(DB_PATH)
        docs = [dict(r) for r in conn.execute("SELECT * FROM documents").fetchall()]
        conn.close()
        corpus = [embed_input(d, t) for d in docs for _, t in chunk_document(d)]
        embedder = embedders.MODELS[model].fit(corpus)
        for d in docs:                                   # embed outside any transaction
            rows = embed_document(embedder, d, "reindex")
            conn = connect(DB_PATH)
            write_chunks(conn, version, model, d["doc_id"], rows)
            conn.close()

        # Catch up documents that arrived while we were building, then switch.
        conn = connect(DB_PATH)
        late = [dict(r) for r in conn.execute("SELECT * FROM documents WHERE updated_at >= ?", (started,))]
        conn.close()
        late_rows = {d["doc_id"]: embed_document(embedder, d, "reindex") for d in late}

        conn = connect(DB_PATH)
        conn.execute("BEGIN IMMEDIATE")
        for doc_id, rows in late_rows.items():
            write_chunks(conn, version, model, doc_id, rows)
        # a document withdrawn during the build must not survive the switch
        conn.execute("DELETE FROM chunks WHERE version=? AND doc_id NOT IN (SELECT doc_id FROM documents)",
                     (version,))
        n = conn.execute("SELECT COUNT(*) FROM chunks WHERE version=?", (version,)).fetchone()[0]
        conn.execute("UPDATE indexes SET status='RETIRED' WHERE status='ACTIVE'")
        conn.execute("UPDATE indexes SET status='ACTIVE', built_at=?, chunks=?, state_json=? WHERE version=?",
                     (time.time(), n, json.dumps(embedder.state()), version))
        conn.execute("COMMIT")
        conn.close()
        REBUILDS.labels(model, "ok").inc()
        print(f"[index] {version} ACTIVE ({n} chunks, {len(late_rows)} caught up, "
              f"{time.time() - started:.1f}s)", flush=True)
        refresh_cache()
        return version
    except Exception as exc:
        conn = connect(DB_PATH)
        conn.execute("UPDATE indexes SET status='FAILED', error=? WHERE version=?", (repr(exc), version))
        conn.execute("DELETE FROM chunks WHERE version=?", (version,))
        conn.close()
        REBUILDS.labels(model, "failed").inc()
        print(f"[index] build {version} FAILED: {exc!r}; active index unchanged", flush=True)
        raise


def index_document(doc: dict, origin: str, event_id: str | None = None,
                   ticket_revision: tuple[str, int] | None = None) -> str:
    """Upsert a document into the active index.

    Returns "indexed", "duplicate" (event_id already processed) or "stale"
    (a newer revision of this ticket was already applied).
    """
    with _lock:
        embedder, version, model = _cache["embedder"], _cache["version"], _cache["model"]
    rows = embed_document(embedder, doc)                 # network call for Ollama: keep it outside the txn
    conn = connect(DB_PATH)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if event_id and already_processed(conn, event_id):
            conn.execute("ROLLBACK")
            return "duplicate"
        if ticket_revision:
            tid, rev = ticket_revision
            seen = conn.execute("SELECT revision FROM incident_revisions WHERE ticket_id=?", (tid,)).fetchone()
            if seen and seen["revision"] >= rev:
                mark_processed(conn, event_id)
                conn.execute("COMMIT")
                return "stale"
            conn.execute("INSERT OR REPLACE INTO incident_revisions VALUES (?,?,'INDEXED',?)",
                         (tid, rev, time.time()))
        upsert_document_row(conn, doc, origin)
        write_chunks(conn, version, model, doc["doc_id"], rows)
        if event_id:
            mark_processed(conn, event_id)
        n = conn.execute("SELECT COUNT(*) FROM chunks WHERE version=?", (version,)).fetchone()[0]
        conn.execute("UPDATE indexes SET chunks=? WHERE version=?", (n, version))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    REINDEX.labels(origin, "ok").inc()
    refresh_cache()
    return "indexed"


def withdraw_incident(ticket_id: str, revision: int, event_id: str) -> str:
    """Remove a resolution that turned out to be wrong (incident reopened)."""
    conn = connect(DB_PATH)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if already_processed(conn, event_id):
            conn.execute("ROLLBACK")
            return "duplicate"
        seen = conn.execute("SELECT revision FROM incident_revisions WHERE ticket_id=?", (ticket_id,)).fetchone()
        if seen and seen["revision"] >= revision:
            mark_processed(conn, event_id)
            conn.execute("COMMIT")
            return "stale"
        doc_id = f"inc-{ticket_id}"
        conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))     # from every index version
        conn.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))
        # tombstone: a late incident.resolved with an older revision must not resurrect it
        conn.execute("INSERT OR REPLACE INTO incident_revisions VALUES (?,?,'WITHDRAWN',?)",
                     (ticket_id, revision, time.time()))
        mark_processed(conn, event_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    REINDEX.labels("incident", "withdrawn").inc()
    refresh_cache()
    return "withdrawn"


# ---------------------------------------------------------------- startup

def load_seed() -> None:
    conn = connect(DB_PATH)
    conn.executescript(DDL + OUTBOX_DDL)
    seeds = [d for p in sorted(SEED_DIR.glob("*.md")) if p.name != "README.md" and (d := parse_seed(p))]
    changed = []
    for d in seeds:
        row = conn.execute("SELECT title, tags, text FROM documents WHERE doc_id=?", (d["doc_id"],)).fetchone()
        if not row or (row["title"], row["tags"], row["text"]) != (d["title"], d["tags"], d["text"]):
            changed.append(d)
    has_index = active_index(conn) is not None
    conn.close()

    if not has_index:
        conn = connect(DB_PATH)
        for d in seeds:
            upsert_document_row(conn, d, "seed")
        conn.close()
        build_index(DEFAULT_MODEL)
    else:
        refresh_cache()
        for d in changed:
            index_document(d, "seed")
        if changed:
            print(f"[seed] re-indexed {len(changed)} changed seed document(s)", flush=True)
    print(f"[seed] {len(seeds)} seed documents; active index {_cache['version']}", flush=True)


def handle_incident_event(event: dict) -> None:
    etype = event.get("event_type")
    data = event.get("data", {})
    ticket_id = data.get("ticket_id") or event.get("aggregate_id")
    revision = int(data.get("revision", 1))
    if not ticket_id:
        EVENTS_CONSUMED.labels(SERVICE, etype or "?", "error").inc()
        raise ValueError(f"{etype} needs ticket_id")

    if etype == "incident.reopened":
        outcome = withdraw_incident(ticket_id, revision, event["event_id"])
        EVENTS_CONSUMED.labels(SERVICE, etype, "processed" if outcome == "withdrawn" else outcome).inc()
        print(f"[ingest] {ticket_id} reopened (rev {revision}): {outcome}", flush=True)
        return

    if not data.get("resolution") or not data.get("summary"):
        EVENTS_CONSUMED.labels(SERVICE, etype, "error").inc()
        raise ValueError("incident.resolved needs summary and resolution")
    # Only staff-written fields are ingested. The user's question is deliberately NOT indexed.
    doc = {
        "doc_id": f"inc-{ticket_id}",
        "title": f"{ticket_id}: {data['summary']}"[:200],
        "source_type": "incident",
        "tags": "resolved incident",
        "text": f"Problem: {data['summary']}\n\nResolution: {data['resolution']}",
        "author": data.get("resolved_by", "on-call"),
    }
    outcome = index_document(doc, "incident", event_id=event["event_id"], ticket_revision=(ticket_id, revision))
    EVENTS_CONSUMED.labels(SERVICE, etype, "processed" if outcome == "indexed" else outcome).inc()
    print(f"[ingest] {ticket_id} resolved (rev {revision}): {outcome}", flush=True)


load_seed()
start_daemon(consume_forever, "knowledge.incident-lifecycle", ["incident.resolved", "incident.reopened"],
             handle_incident_event, name="knowledge-incident-consumer")


# ---------------------------------------------------------------- API

class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(3, ge=1, le=20)


class DocumentIn(BaseModel):
    doc_id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1)
    source_type: str = Field(pattern="^(runbook|faq|incident)$")
    tags: str = ""
    text: str = Field(min_length=1)


class RebuildIn(BaseModel):
    embedding_model: str = DEFAULT_MODEL


class FaultIn(BaseModel):
    latency_ms: int = Field(0, ge=0, le=60000)
    error_rate: float = Field(0.0, ge=0.0, le=1.0)


@app.post("/search")
def search(body: SearchIn):
    if _fault["latency_ms"]:
        time.sleep(_fault["latency_ms"] / 1000)
    if _fault["error_rate"] and random.random() < _fault["error_rate"]:
        raise HTTPException(503, "injected fault")
    with _lock:
        embedder, version, model, chunks = _cache["embedder"], _cache["version"], _cache["model"], _cache["chunks"]
    if embedder is None:
        raise HTTPException(503, "index not ready")
    try:
        q = embed_texts(embedder, [body.query], "query")[0]
    except Exception as exc:
        raise HTTPException(503, f"embedding model unavailable: {exc!r}")
    best: dict[str, tuple[float, dict]] = {}
    for c in chunks:
        s = embedders.similarity(q, c["vector"])
        if s > 0 and (c["doc_id"] not in best or s > best[c["doc_id"]][0]):
            best[c["doc_id"]] = (s, c)
    ranked = sorted(best.values(), key=lambda x: x[0], reverse=True)[: body.top_k]
    return {"index_version": version, "embedding_model": model, "results": [
        {"doc_id": c["doc_id"], "title": c["title"], "source_type": c["source_type"],
         "origin": c["origin"], "author": c["author"], "embedding_model": c["model"],
         "text": c["text"], "score": round(s, 4)} for s, c in ranked]}


@app.get("/index")
def index_info():
    conn = connect(DB_PATH)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT version, model, status, started_at, built_at, chunks, error FROM indexes ORDER BY started_at DESC")]
        docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    finally:
        conn.close()
    active = next((r for r in rows if r["status"] == "ACTIVE"), None)
    return {"active_version": active and active["version"], "embedding_model": active and active["model"],
            "documents": docs, "chunks": active and active["chunks"],
            "built_at": active and active["built_at"], "versions": rows}


@app.post("/documents", status_code=202)
def add_document(body: DocumentIn):
    index_document(body.model_dump(), "api")
    return {"doc_id": body.doc_id, "index_version": _cache["version"]}


@app.get("/documents")
def list_documents():
    """Every article currently in the knowledge base (seed files + learned from resolved tickets)."""
    conn = connect(DB_PATH)
    try:
        rows = conn.execute("SELECT doc_id, title, source_type, tags, text, origin, author, updated_at "
                            "FROM documents ORDER BY source_type, title").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


@app.get("/", include_in_schema=False)
def browse():
    return FileResponse(Path(__file__).parent / "static" / "browse.html")


@app.get("/documents/{doc_id}")
def get_document(doc_id: str):
    conn = connect(DB_PATH)
    try:
        row = conn.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "document not found")
    return dict(row)


@app.post("/index/rebuild", status_code=202)
def rebuild(body: RebuildIn):
    if body.embedding_model not in embedders.MODELS:
        raise HTTPException(400, f"unknown model; choose one of {sorted(embedders.MODELS)}")
    conn = connect(DB_PATH)
    building = conn.execute("SELECT version FROM indexes WHERE status='BUILDING'").fetchone()
    conn.close()
    if building:
        raise HTTPException(409, f"build already in progress: {building['version']}")
    threading.Thread(target=lambda: _safe_build(body.embedding_model), daemon=True).start()
    return {"status": "BUILDING", "embedding_model": body.embedding_model,
            "active_version": _cache["version"], "note": "searches use the active index until the build completes"}


def _safe_build(model: str) -> None:
    try:
        build_index(model)
    except Exception:
        pass   # already logged and recorded as FAILED; active index untouched


@app.post("/admin/fault")
def set_fault(body: FaultIn):
    _fault.update(body.model_dump())
    print(f"[fault] search fault injection set to {_fault}", flush=True)
    return _fault


@app.get("/health")
def health():
    return {"status": "ok" if _cache["version"] else "starting", "service": SERVICE,
            "index_version": _cache["version"], "chunks": len(_cache["chunks"])}
