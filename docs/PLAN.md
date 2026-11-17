# Build plan

| Phase | Scope | Brief requirement covered | Status |
|---|---|---|---|
| 0 | Repo, shared lib, 4 service stubs, RabbitMQ, Prometheus, Grafana, one-command startup | Containerised, observable | Done |
| 1 | Ticket Service (OPEN/ACKED/RESOLVED + outbox); Knowledge Base (vector index, seed synthetic runbooks/FAQs, retrieval API, idempotent `incident.resolved` consumer) | Async path doing real work; outbox; idempotent consumer | Next |
| 2 | Chat/RAG: retrieval behind timeout + retry + circuit breaker; confidence threshold (tunable, logged); simulated LLM + optional Ollama; token-cost metrics; prompt-injection guard | Resilience; cost; non-determinism; injection defence | |
| 3 | On-Call: roster (strong consistency), consume `incident.opened`, simulated paging, ack timers, escalation chain, idempotent ack | Consistency decisions | |
| 4 | Failure-injection scripts (kill KB, kill RabbitMQ, duplicate events) + runbook | Resilience demonstrated under injected failure | |
| 5 | Embedding-model/index versioning (v1 built-in, v2 Ollama) with full re-index + cutover; threshold calibration log | Model/index versioning as a deployment quantum | |
| 6 | Report, C4 diagrams, 5 ADRs (one per member), AI-disclosure appendix, contribution statement | Deliverables | |

## Event catalogue (topic exchange `support.events`)

| Event | Producer | Consumers |
|---|---|---|
| `incident.opened` | Tickets (outbox) | On-Call |
| `incident.acked` | Tickets (outbox) | On-Call (stops escalation timer) |
| `incident.resolved` | Tickets (outbox) | Knowledge Base (re-index) |

## Open design questions (to settle in ADRs — owners decide, not AI)

- How is "confidence" computed (retrieval similarity, grounding check, both)?
- Chat -> Tickets escalation: synchronous HTTP with idempotency key, or async?
- Vector store: SQLite + brute-force cosine vs. a dedicated store (Qdrant/Chroma)?
- What happens to in-flight queries during an embedding-model cutover?
