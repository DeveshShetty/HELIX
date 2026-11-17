# HELIX: AI Support Agent & Incident Escalation Platform

COMP41720 Distributed Systems, Group 18, Option B (AI-native).

A retrieval-augmented support agent answers questions about **Helix Cloud**, a
fictional SaaS platform, from runbooks, FAQs and past incidents. When it cannot
ground an answer confidently it opens a ticket, which pages the on-call
engineer and escalates up the chain until someone acks. Once resolved, the
engineer's fix is re-indexed, so the same question is answered directly next
time. If the fix turns out to be wrong, reopening the ticket withdraws it.

## Quick start (one command)

```bash
docker compose up --build
```

Runs fully offline at zero cost. All data is synthetic.

| Open | URL |
|---|---|
| **Chat app** | http://localhost:8001 |
| **On-call console** | http://localhost:8003 |
| Knowledge base (browse articles) | http://localhost:8004 |
| Grafana dashboard | http://localhost:3000 |
| RabbitMQ (guest / guest) | http://localhost:15672 |
| Prometheus | http://localhost:9090 |
| API docs | http://localhost:8001/docs · 8002/docs · 8003/docs · 8004/docs |

Stop: `docker compose down` · wipe all data: `docker compose down -v`

## Try it

1. Chat app: click the example questions (answered, escalated, poisoned document, injection).
2. Ask something it can't answer, e.g. *"do you offer a student discount"*: it opens a ticket.
3. In the chat, click **Chat with a support agent now**. On the on-call console the ticket shows
   **CUSTOMER WAITING IN CHAT**: **Ack**, **Join live chat** and talk to the customer. You'll also see
   the bot's part of the conversation and why it got stuck.
4. **End chat & draft summary**, review the pre-filled summary and fix, then **Resolve**.
5. Ask the same question again: answered, citing the resolved incident.
6. **Reopen** the ticket: the answer is withdrawn and the question escalates again.
7. Break things: `python scripts/demo.py kb-down` (see [docs/RUNBOOK.md](docs/RUNBOOK.md)).
8. Calibrate: `python eval/run_eval.py` and `python eval/flip_test.py`.

## Architecture

```
              ┌──────────── sync HTTP (deadline, retry budget, circuit breaker) ───────────┐
 User ─► Chat / RAG ──────────────────────────────────────────────────────────► Knowledge Base
            │  escalate (Idempotency-Key, local outbox)                                ▲
            ▼                                                                         │ incident.resolved
       Incident / Ticket ──outbox──► RabbitMQ (support.events) ──────────────────────┤ incident.reopened
            ▲                              │ incident.opened / acked / resolved / reopened
            │ ack / resolve / reopen       ▼
            └────────────────────── On-Call & Escalation ◄── responders (console)
```

| Service | Port | Owns | Key patterns |
|---|---|---|---|
| Chat / RAG | 8001 | decision log, escalation outbox | circuit breaker, deadline + retry budget, confidence threshold per embedding model, injection guard, token budget + daily ceiling |
| Incident / Ticket | 8002 | tickets (system of record) | transactional outbox, idempotency keys, revisioned state machine |
| On-Call & Escalation | 8003 | roster, pages | optimistic-concurrency roster, durable ack timers, idempotent consumer + ack |
| Knowledge Base | 8004 | documents, versioned vector index | blue/green index rebuild, idempotent revision-ordered consumer |

Each service has its own SQLite database (database-per-service) and its own Dockerfile.

## AI configuration

| Setting | Default | Notes |
|---|---|---|
| `LLM_MODE` | `simulated` | `ollama` uses `llama3.2:1b` on the host (`ollama serve`) |
| `CONFIDENCE_THRESHOLDS` | `{"hash-v1": 0.40}` | per embedding model; from `eval/results/threshold_curve.csv` |
| `EMBEDDING_MODEL` | `hash-v1` | built in; `nomic-embed-text` via Ollama and `POST /index/rebuild` |
| `DAILY_COST_CEILING_USD` | `0.50` | notional; reached → escalate |
| `MAX_PROMPT_TOKENS` | `1500` | per query; exceeded → escalate |
| `ACK_TIMEOUT_SECONDS` | `30` | page escalates to the next responder after this |

Set them in a `.env` file next to `docker-compose.yml`.

## Repository

```
common/              RabbitMQ helpers, topology, outbox, circuit breaker, metrics
services/<name>/     one independently deployable service each
services/knowledge/seed/   the Helix Cloud knowledge base (39 articles, synthetic)
eval/                labelled questions, calibration curve, flip test, results/
scripts/             failure-injection demos, event replay
monitoring/          Prometheus config, Grafana dashboard
docs/                CONTRACTS, RUNBOOK, EVIDENCE, PLAN, ADRs, AI disclosure
```
