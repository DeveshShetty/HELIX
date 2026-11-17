# AI Support Agent & Incident Escalation Platform

COMP41720 Distributed Systems — Group 18 — Option B (AI-native).

A retrieval-augmented support agent answers questions from a knowledge base of
runbooks, FAQs and past incidents. When it cannot ground an answer confidently
it opens a ticket, which is paged to on-call; once resolved, the fix is
re-indexed so the same question is answered directly next time.

## Quick start (one command)

```bash
docker compose up --build
```

Runs fully offline at zero cost (simulated LLM by default; Ollama optional).

| Component | URL |
|---|---|
| Chat / RAG Service | http://localhost:8001/docs |
| Incident / Ticket Service | http://localhost:8002/docs |
| On-Call & Escalation Service | http://localhost:8003/docs |
| Knowledge Base Service | http://localhost:8004/docs |
| RabbitMQ UI (guest/guest) | http://localhost:15672 |
| Prometheus | http://localhost:9090 |
| Grafana dashboard | http://localhost:3000 |

Stop and wipe all data: `docker compose down -v`

## Services and owners

| Service | Owner | Key pattern |
|---|---|---|
| Chat / RAG Support | _TBD_ | Circuit breaker on retrieval + confidence threshold |
| Incident / Ticket | _TBD_ | Transactional outbox |
| On-Call & Escalation | _TBD_ | Strongly consistent roster + idempotent ack |
| Knowledge Base / Ingestion | _TBD_ | Async, idempotent re-indexing |

## Repository layout

```
common/            shared library: RabbitMQ, outbox, circuit breaker, metrics
services/<name>/   one folder + Dockerfile per independently deployable service
monitoring/        Prometheus scrape config, Grafana provisioning + dashboard
docs/              plan, ADRs, AI-disclosure log, runbook
```

See [docs/PLAN.md](docs/PLAN.md) for the phased build plan.
