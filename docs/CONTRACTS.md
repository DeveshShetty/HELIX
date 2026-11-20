# Service contracts

The APIs and events between services. Code that disagrees with this file is a
bug in one or the other. All bodies are JSON, timestamps are Unix epoch
seconds. Every service also exposes `GET /health` and `GET /metrics/`.

| Service | Port | UI | Owner |
|---|---|---|---|
| Chat / RAG | 8001 | chat app at `/` | Devesh Shetty |
| Incident / Ticket | 8002 | — | Devesh Shetty |
| On-Call & Escalation | 8003 | on-call console at `/` | Devesh Shetty |
| Knowledge Base | 8004 | article browser at `/` | Devesh Shetty |
| Summariser | 8005 | — | Devesh Shetty |

---

## 1. Knowledge Base (8004)

### `POST /search`
```json
{ "query": "payment api returns 502", "top_k": 3 }
```
```json
{
  "index_version": "hash-v1-20261117T184243",
  "embedding_model": "hash-v1",
  "results": [{
    "doc_id": "rb-payments-502", "title": "Payment API returns 502 Bad Gateway",
    "source_type": "runbook",              // runbook | faq | incident
    "origin": "seed",                      // seed | api | incident
    "author": "kb-editors",                // for incidents: the on-call responder
    "embedding_model": "hash-v1",          // model that produced this chunk's vector
    "text": "…best chunk of the document…",
    "score": 0.46                          // cosine similarity; scale depends on the model
  }]
}
```
One result per document (its best chunk), sorted by score.

### `GET /index`
Active version, model, counts, and every version with its status
(`BUILDING | ACTIVE | RETIRED | FAILED`).

### `POST /index/rebuild` → 202
```json
{ "embedding_model": "hash-v1" }          // or "nomic-embed-text" (needs Ollama)
```
Builds a new version alongside the active one, catches up documents that
arrived during the build, then switches in one transaction. 409 if a build is
already running. A failed build leaves the active index untouched.

### `POST /documents` → 202, `GET /documents/{doc_id}`
Upsert by `doc_id` (idempotent).

### `POST /admin/fault` (demo only)
```json
{ "latency_ms": 5000, "error_rate": 0.0 }
```

### Consumes
| Event | Action |
|---|---|
| `incident.resolved` | Upsert `inc-<ticket_id>` from the **staff-written** `summary` + `resolution` only, with personal data removed; skipped if `add_to_knowledge_base` is false |
| `incident.reopened` | Withdraw `inc-<ticket_id>` from every index version |

Idempotent on `event_id`; ordered by ticket `revision` (an older revision is
ignored, so a late `resolved` cannot resurrect a withdrawn answer).

---

## 2. Incident / Ticket (8002)

State machine: `OPEN → ACKED → RESOLVED`, `OPEN → RESOLVED`, `RESOLVED → OPEN` (reopen).
Illegal transition → `409`. Repeating the current state → `200`, unchanged, no event.
Every resolve and reopen increments `revision`.

### `POST /tickets` (header `Idempotency-Key` required)
```json
{ "title": "…", "question": "…", "severity": "medium",
  "conversation_id": "c-1", "confidence": 0.31, "retrieved_doc_ids": [] }
```
`201` new, or `200` with the same ticket if the key was seen before.

### `GET /tickets/{id}`, `GET /tickets?status=OPEN&limit=100`

### `POST /tickets/{id}/ack`
```json
{ "responder_id": "alice" }
```

### `POST /tickets/{id}/resolve`
```json
{ "responder_id": "alice", "summary": "Exporting invoices to CSV",
  "resolution": "Go to Billing > Invoices …" }
```
`summary` is written by the responder. It replaces the user's question in
everything that gets re-indexed. Optional `"add_to_knowledge_base": false` resolves a
one-off issue without indexing it (default `true`).

### `POST /tickets/{id}/reopen`
```json
{ "responder_id": "bob", "reason": "fix did not work" }
```

### `GET /outbox` (demo/debug): `{ "pending": 0, "published": 16 }`

---

## 3. On-Call & Escalation (8003)

### `GET /roster`, `PUT /roster`
```json
{ "version": 3, "chain": ["alice", "bob", "carol"], "updated_by": "admin" }
```
`PUT` must send the `version` it read; stale → `409`.

### `GET /pages?ticket_id=T-000001`
```json
[{ "page_id": "P-2", "ticket_id": "T-000001", "responder_id": "bob", "level": 1,
   "status": "PENDING", "sent_at": 0, "deadline": 0, "acked_at": null }]
```
`status`: `PENDING | ACKED | TIMED_OUT | CANCELLED`.

### `POST /pages/{page_id}/ack`
`{ "responder_id": "bob" }`. Idempotent. Acks the ticket first, then records locally.

### `POST /incidents/{ticket_id}/resolve`, `POST /incidents/{ticket_id}/reopen`
Same bodies as the Ticket Service; forwarded with timeout + retry.

### Live chat (relayed to the Chat service, which owns conversations)
`GET /tickets/{ticket_id}/conversation`, `POST /conversations/{cid}/messages` `{responder_id, text}`,
`POST /conversations/{cid}/close` `{responder_id}` → draft summary.

### `GET /console/data`
Everything the console shows. Falls back to the local ticket projection if the
Ticket Service is unreachable.

### Behaviour
- `incident.opened` / `incident.reopened` → page `chain[0]`, deadline `now + ACK_TIMEOUT_SECONDS`.
- Past the deadline → `TIMED_OUT`, page the next level (the last level repeats).
- `incident.acked` / `incident.resolved` → cancel pending pages.
- Deadlines are stored in the database: a restart resumes escalation.

---

## 4. Chat / RAG (8001)

### `POST /chat`
```json
{ "message": "payment api is giving 502", "conversation_id": "c-1", "dry_run": false }
```
`dry_run: true` runs the same decision path but never opens a ticket (used by `eval/`).

```json
{
  "decision_id": "d-…", "conversation_id": "c-1",
  "decision": "answer",                    // answer | escalate | refused
  "reason": "grounded",
  "answer": "…",
  "confidence": 0.74, "threshold": 0.40,
  "citations": ["rb-payments-502"],
  "sources": [{ "doc_id": "rb-payments-502", "origin": "seed", "author": "kb-editors",
                "embedding_model": "hash-v1" }],
  "ticket_id": null, "escalation_pending": false, "degraded": false,
  "model": "simulated", "prompt_version": "support-v2",
  "index_version": "hash-v1-…", "embedding_model": "hash-v1",
  "tokens": { "prompt": 412, "completion": 96 }, "cost_usd": 0.00031
}
```

| `reason` | `decision` | Meaning |
|---|---|---|
| `grounded` | answer | confidence ≥ threshold, answer built from the top passage |
| `small_talk`, `needs_clarification` | answer | greeting / vague message, no search, no ticket |
| `injection_detected` | refused | the user's message tried to override instructions |
| `low_confidence` | escalate | confidence < threshold for this embedding model |
| `kb_unavailable` | escalate | Knowledge down / circuit open (`degraded: true`) |
| `uncalibrated_index` | escalate | no threshold for the active index version (or, unless `THRESHOLD_STRICT=1`, its embedding model). Decisions record `threshold_key` (`index:<version>` or `model:<name>`) |
| `daily_budget_exhausted` | escalate | daily token or cost ceiling reached |
| `token_budget_exceeded` | escalate | prompt would exceed the per-query token budget |
| `unsafe_output` | escalate | generated text matched an injection pattern |

If the Ticket Service is unreachable, `escalation_pending: true` and the
escalation is delivered later by the outbox worker (same `Idempotency-Key`).

### `GET /decisions?limit=50`, `GET /decisions/{id}`, `POST /decisions/{id}/feedback`, `GET /config`

### Conversations and live hand-off
Every message is stored (`conversations`, `messages` tables in Chat's database); bot
messages link to their decision, so the transcript records why the bot got stuck.

| Endpoint | Purpose |
|---|---|
| `POST /conversations/{cid}/leave` `{reason: customer\|resolved}` | stop waiting for an agent: `WAITING_AGENT → BOT` (ticket stays open). On-Call calls it with `resolved` when a ticket is resolved from the console while the customer is still queued |
| `POST /conversations/{cid}/handoff` | customer chose "chat with an agent" (needs a ticket); status `BOT → WAITING_AGENT` |
| `POST /conversations/{cid}/messages` `{role: user\|agent, author, text}` | live message; the first agent message makes it `LIVE` |
| `GET /conversations/{cid}?after=<seq>` | transcript (+ new messages since `seq`, for polling) |
| `GET /conversations/by-ticket/{ticket_id}` | transcript for a ticket |
| `GET /conversations?status=WAITING_AGENT,LIVE` | open live chats |
| `POST /conversations/{cid}/close` `{agent_id}` | end the chat → `CLOSED`; writes `conversation.closed` (with the transcript) to Chat's outbox in the same transaction |

While a conversation is `WAITING_AGENT` or `LIVE`, `POST /chat` stores the message and
returns `decision: "live"` without running the bot.

---

## 5. Summariser (8005)

Consumes `conversation.closed`, drafts PROBLEM/FIX (Ollama few-shot prompt
`summary-v2-fewshot`, or extractive), runs automatic checks, stores a PENDING draft.

| Endpoint | Purpose |
|---|---|
| `GET /drafts?status=PENDING` | drafts waiting for an agent (with `checks`, `method`, `transcript`) |
| `GET /drafts/{draft_id}` | one draft |
| `POST /drafts/{draft_id}/approve` `{responder_id, summary, resolution, add_to_knowledge_base}` | re-checks the (edited) text, then resolves the ticket → `incident.resolved` → Knowledge. `409` if the ticket is already resolved (a repeat resolve emits no event, so the article would silently not be indexed) |
| `POST /drafts/{draft_id}/reject` `{responder_id, reason}` | discard |

Checks: `grounding` (at least 60% of an LLM fix's words must appear in the agent's
messages, else the extractive draft is used), `llm_format`, `llm_available`,
`personal_data` (removed automatically), `injection`, `has_fix`. On-Call relays these as
`GET /summaries`, `POST /summaries/{id}/approve|reject` for the console.

## 6. Events (topic exchange `support.events`)

Envelope: `{ event_id, event_type, aggregate_id, correlation_id, occurred_at, data }`.
At-least-once; every consumer dedupes on `event_id`.

| Event | Producer | Consumers | `data` |
|---|---|---|---|
| `incident.opened` | Tickets | On-Call | `ticket_id, revision, title, question, severity, created_at` |
| `incident.acked` | Tickets | On-Call | `ticket_id, revision, responder_id, acked_at` |
| `incident.resolved` | Tickets | Knowledge, On-Call | `ticket_id, revision, summary, resolution, resolved_by, resolved_at, add_to_knowledge_base` |
| `incident.reopened` | Tickets | Knowledge, On-Call | `ticket_id, revision, reopened_by, reason, reopened_at` |
| `conversation.closed` | Chat (outbox) | Summariser | `conversation_id, ticket_id, agent_id, closed_at, messages[]` (event-carried transcript) |

Queues and bindings are declared by every service at startup from
`common/topology.py` (`oncall.incident-lifecycle`, `knowledge.incident-lifecycle`, `summariser.conversations`),
so an event never waits on a consumer having started first.

---

## 7. Metrics

All services: `http_requests_total`, `http_request_duration_seconds`, `events_consumed_total`.

| Service | Metrics |
|---|---|
| Chat | `chat_decisions_total{decision,reason}`, `chat_confidence`, `llm_tokens_total`, `llm_cost_usd_total`, `llm_daily_spend_usd`, `llm_daily_ceiling_usd`, `escalations_pending`, `circuit_breaker_state{breaker}` |
| Tickets | `tickets_created_total`, `ticket_transitions_total`, `outbox_backlog` |
| Knowledge | `kb_chunks{index_version}`, `kb_reindex_total`, `kb_index_builds_total`, `embedding_tokens_total`, `embedding_cost_usd_total` |
| Summariser | `summaries_drafted_total{method}`, `summary_checks_failed_total{check}`, `summaries_decided_total`, `summaries_pending`, `summariser_llm_tokens_total`, `summariser_llm_cost_usd_total` |
| On-Call | `pages_sent_total{level}`, `escalations_total{from_level}`, `pages_pending`, `page_acks_total` |
