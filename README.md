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
| **Chat app (start here)** | **http://localhost:8001** |
| Chat / RAG Service API | http://localhost:8001/docs |
| Incident / Ticket Service | http://localhost:8002/docs |
| On-Call & Escalation Service | http://localhost:8003/docs |
| Knowledge Base Service | http://localhost:8004/docs |
| RabbitMQ UI (guest/guest) | http://localhost:15672 |
| Prometheus | http://localhost:9090 |
| Grafana dashboard | http://localhost:3000 |

Stop and wipe all data: `docker compose down -v`

## Try it

1. Open http://localhost:8001 and click the example questions.
2. Check answer-vs-escalate accuracy: `python eval/run_eval.py --sweep`
3. Kill the knowledge base and keep chatting: `docker compose stop knowledge`.
   Replies escalate and are marked *degraded*; after 3 failures the circuit
   opens. `docker compose start knowledge` and it recovers by itself.
4. Real LLM instead of the simulated one (needs Ollama running with
   `llama3.2:1b`): set `LLM_MODE=ollama` in `.env`, then `docker compose up -d chat`.

> Knowledge and Tickets are currently **temporary mocks** so Chat can be
> demoed; their owners are replacing them (see docs/TASKS.md).

## Services and owners

| Service | Owner | Key pattern |
|---|---|---|
| Chat / RAG Support | Devesh Shetty | Circuit breaker on retrieval + confidence threshold |
| Incident / Ticket | Aditya Mahendru | Transactional outbox |
| On-Call & Escalation | Raheel Vasudeva | Strongly consistent roster + idempotent ack |
| Knowledge Base / Ingestion | Liu Yang | Async, idempotent re-indexing |

## Repository layout

```
common/            shared library: RabbitMQ, outbox, circuit breaker, metrics
services/<name>/   one folder + Dockerfile per independently deployable service
monitoring/        Prometheus scrape config, Grafana provisioning + dashboard
docs/              plan, ADRs, AI-disclosure log, runbook
```

- [docs/PLAN.md](docs/PLAN.md): phased build plan
- [docs/CONTRACTS.md](docs/CONTRACTS.md): APIs and events between services (read this first)
- [docs/TASKS.md](docs/TASKS.md): who does what, and how to get started
