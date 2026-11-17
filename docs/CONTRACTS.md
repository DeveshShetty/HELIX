# Service contracts

This file is the agreement between services. **Change it only by PR, and tag
the owners of every service the change touches.** Code that disagrees with
this file is a bug in the code, or this file needs a PR first.

All bodies are JSON. All timestamps are Unix epoch seconds (float). IDs are
strings. Every service also exposes `GET /health` and `GET /metrics/`.

| Service | Port | Base URL (inside compose) | Owner |
|---|---|---|---|
| Chat / RAG | 8001 | `http://chat:8001` | Devesh Shetty |
| Incident / Ticket | 8002 | `http://tickets:8002` | Aditya Mahendru |
| On-Call & Escalation | 8003 | `http://oncall:8003` | Raheel Vasudeva |
| Knowledge Base | 8004 | `http://knowledge:8004` | Liu Yang |

---

## 1. Knowledge Base Service (8004) — owner: Liu Yang

### `POST /search`
Called synchronously by Chat (behind Chat's circuit breaker).
```json
// request
{ "query": "payment api returns 502", "top_k": 3 }

// 200 response
{
  "index_version": "v1-hash-2026-11-17T12:00:00",
  "embedding_model": "hash-v1",
  "results": [
    {
      "doc_id": "rb-payments-502",
      "title": "Payment API returns 502 Bad Gateway",
      "source_type": "runbook",          // runbook | faq | incident
      "text": "…the matching chunk…",
      "score": 0.83                      // cosine similarity, 0..1, higher = closer
    }
  ]
}
```
Results are sorted by `score` descending. An empty `results` list is valid.

### `GET /index`
```json
{ "active_version": "v1-hash-…", "embedding_model": "hash-v1",
  "documents": 37, "chunks": 120, "built_at": 1763380000.0 }
```

### `POST /documents` (manual ingestion; also used by the seed loader)
```json
{ "doc_id": "faq-2fa-reset", "title": "…", "source_type": "faq", "text": "…" }
```
`202 Accepted`. Upsert by `doc_id`, so it is idempotent.

### `POST /index/rebuild` (Phase 5)
```json
// request
{ "embedding_model": "nomic-embed-text" }
// 202 response
{ "target_version": "v2-nomic-…", "status": "BUILDING" }
```
Builds a new index alongside the active one; switches `active_version` only
when the build completes. Searches keep hitting the old index meanwhile.

### Consumes
- `incident.resolved` → indexes `question + resolution` as a document with
  `doc_id = "inc-<ticket_id>"`, `source_type = "incident"`. Must be idempotent
  (dedupe on `event_id`; upsert on `doc_id`).

---

## 2. Incident / Ticket Service (8002) — owner: Aditya Mahendru

State machine: `OPEN → ACKED → RESOLVED` (also `OPEN → RESOLVED`).
Any other transition → `409 Conflict`. Repeating the transition the ticket is
already in (ack an ACKED ticket, resolve a RESOLVED one) → `200` with the
current ticket, unchanged, and **no new event** (idempotent).

### `POST /tickets`
Header `Idempotency-Key: <string>` is **required**. Chat uses
`chat-<decision_id>`, so a retried escalation never creates two tickets.
```json
// request
{
  "title": "Customer cannot export invoices as CSV",
  "question": "how do I export invoices to csv?",
  "severity": "medium",                 // low | medium | high
  "conversation_id": "c-123",
  "confidence": 0.31,
  "retrieved_doc_ids": ["faq-billing-invoices"]
}
// 201 Created (new) or 200 OK (same Idempotency-Key seen before)
{ "ticket_id": "T-000123", "status": "OPEN", "created_at": 1763380000.0 }
```

### `GET /tickets/{ticket_id}` and `GET /tickets?status=OPEN`
Full ticket:
```json
{ "ticket_id": "T-000123", "status": "ACKED", "title": "…", "question": "…",
  "severity": "medium", "conversation_id": "c-123", "confidence": 0.31,
  "retrieved_doc_ids": [], "acked_by": "alice", "resolution": null,
  "resolved_by": null, "created_at": 0, "acked_at": 0, "resolved_at": null }
```

### `POST /tickets/{ticket_id}/ack`
```json
{ "responder_id": "alice" }
```

### `POST /tickets/{ticket_id}/resolve`
```json
{ "responder_id": "alice", "resolution": "Go to Billing → Invoices → Export…" }
```

### Publishes (via transactional outbox, same DB transaction as the state change)
See §5.

---

## 3. On-Call & Escalation Service (8003) — owner: Raheel Vasudeva

### `GET /roster` / `PUT /roster`
Strongly consistent: `PUT` must send the `version` it read; a stale version →
`409 Conflict` (optimistic concurrency).
```json
{ "version": 3,
  "chain": ["alice", "bob", "carol"] }   // primary, secondary, manager
```

### `GET /pages?ticket_id=T-000123`
```json
[ { "page_id": "P-1", "ticket_id": "T-000123", "responder_id": "alice",
    "level": 0, "status": "PENDING", "sent_at": 0, "acked_at": null } ]
```
`status`: `PENDING | ACKED | TIMED_OUT | CANCELLED`.

### `POST /pages/{page_id}/ack`
```json
{ "responder_id": "alice" }
```
Idempotent. Calls Tickets `POST /tickets/{id}/ack` (with timeout + retry),
stops the escalation timer for that ticket.

### Behaviour
- On `incident.opened`: page `chain[0]`. If not acked within
  `ACK_TIMEOUT_SECONDS` (default `30`), mark the page `TIMED_OUT` and page the
  next level. After the last level, keep re-paging the last level.
- On `incident.acked` (e.g. someone acked directly in Tickets): cancel pending
  pages for that ticket.
- Timers must survive a service restart (store deadlines in the DB, not only
  in memory).

### Consumes
`incident.opened`, `incident.acked`, `incident.resolved` (cancel pages).

---

## 4. Chat / RAG Support Service (8001) — owner: Devesh Shetty

### `POST /chat`
```json
// request
{ "message": "payment api is giving 502", "conversation_id": "c-123" }

// 200 response
{
  "decision_id": "d-…",
  "conversation_id": "c-123",
  "decision": "answer",                 // answer | escalate | refused
  "answer": "…",                        // the user-facing text, always present
  "confidence": 0.78,
  "threshold": 0.55,
  "citations": ["rb-payments-502"],
  "ticket_id": null,                    // set when decision = escalate
  "degraded": false,                    // true when KB was unreachable / circuit open
  "reason": "grounded",                 // grounded | low_confidence | kb_unavailable | injection_detected
  "model": "simulated",
  "index_version": "v1-hash-…",
  "tokens": { "prompt": 412, "completion": 96 },
  "cost_usd": 0.00031
}
```
Rule: **degrade toward escalation, never toward a guessed answer.** If the KB
is unavailable the response is `decision = escalate`, `degraded = true`.

### `GET /decisions?limit=50`
Recent decisions with confidence, for threshold calibration.

### `POST /decisions/{decision_id}/feedback`
```json
{ "human_contradicted": true, "note": "answer was out of date" }
```

---

## 5. Events (RabbitMQ topic exchange `support.events`)

Envelope (built by `common.messaging.new_event`):
```json
{ "event_id": "uuid", "event_type": "incident.opened", "aggregate_id": "T-000123",
  "correlation_id": "T-000123", "occurred_at": 0.0, "data": { } }
```
Delivery is **at-least-once**: every consumer dedupes on `event_id` using the
`processed_events` table (`common.db.already_processed / mark_processed`).

| `event_type` (= routing key) | Producer | Consumers | `data` |
|---|---|---|---|
| `incident.opened` | Tickets | On-Call | `ticket_id, title, question, severity, created_at` |
| `incident.acked` | Tickets | On-Call | `ticket_id, responder_id, acked_at` |
| `incident.resolved` | Tickets | Knowledge, On-Call | `ticket_id, title, question, resolution, resolved_by, resolved_at` |

Queue names: `<consumer>.<event>` e.g. `knowledge.incident-resolved`,
`oncall.incident-opened`.

---

## 6. Metrics (names agreed so the shared dashboard works)

Every service gets `http_requests_total`, `http_request_duration_seconds`
automatically from `common.observability.instrument`.

| Metric | Type | Labels | Owner |
|---|---|---|---|
| `events_consumed_total` | counter | `service, event_type, outcome` (`processed / duplicate / error`) | all consumers |
| `outbox_backlog` | gauge | `service` | Tickets |
| `circuit_breaker_state` | gauge | `breaker` | Chat |
| `chat_decisions_total` | counter | `decision, reason` | Chat |
| `chat_confidence` | histogram | — | Chat |
| `llm_tokens_total` | counter | `model, kind` (`prompt / completion`) | Chat |
| `llm_cost_usd_total` | counter | `model` | Chat |
| `kb_chunks` | gauge | `index_version` | Knowledge |
| `kb_reindex_total` | counter | `outcome` | Knowledge |
| `pages_sent_total` | counter | `level` | On-Call |
| `escalations_total` | counter | `from_level` | On-Call |
