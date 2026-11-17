# Task breakdown

Everyone owns **one service + one ADR + one shared task**. Work on your own
branch, open a Pull Request into `main`, and get one teammate to review it.
Commit little and often: the repo history is marking evidence.

Before you start: read [CONTRACTS.md](CONTRACTS.md). That is what the other
services expect from yours.

## Getting started (everyone)

```bash
git clone https://github.com/DeveshShetty/HELIX.git
cd HELIX
git checkout -b feature/<your-service>
docker compose up --build          # everything should come up healthy
```
Your service lives in `services/<name>/main.py`. Use the shared helpers in
`common/` (outbox, idempotent consumer, circuit breaker, metrics); they are
adapted from our Lab 2 and Lab 3 code.

Log every significant AI assist in `docs/ai-disclosure.md` as you go (the
appendix is mandatory and marked). Write your ADR yourself: the brief says
trade-off reasoning cannot be delegated to AI.

---

## Devesh Shetty: Chat / RAG Service (`feature/chat`)

- [ ] `POST /chat` per contract §4
- [ ] KB client: timeout + retry with backoff + circuit breaker (`common.circuit_breaker`)
- [ ] Confidence score + `CONFIDENCE_THRESHOLD` env var (tunable, logged per decision)
- [ ] Escalate via `POST /tickets` with `Idempotency-Key: chat-<decision_id>`
- [ ] KB down / circuit open → escalate with `degraded: true`, never guess
- [ ] LLM: simulated (default) + Ollama `llama3.2:1b` (`LLM_MODE=ollama`)
- [ ] Token + notional cost metrics (`llm_tokens_total`, `llm_cost_usd_total`)
- [ ] Prompt-injection guard (user input AND retrieved documents)
- [ ] `GET /decisions`, `POST /decisions/{id}/feedback` (threshold calibration)
- [ ] **ADR:** how confidence is computed and how the threshold is set
- [ ] **Shared:** docker-compose / integration, keep `main` running

## Liu Yang: Knowledge Base Service (`feature/knowledge`)

- [ ] Load `services/knowledge/seed/*.md` on startup (format: see `seed/README.md`)
- [ ] Chunk + embed with built-in `hash-v1` embedder; store vectors in SQLite
- [ ] `POST /search`, `GET /index`, `POST /documents` per contract §1
- [ ] Consume `incident.resolved` → upsert `inc-<ticket_id>`; idempotent on `event_id`
- [ ] Metrics: `kb_chunks`, `kb_reindex_total`, `events_consumed_total`
- [ ] Phase 5: `POST /index/rebuild` to `nomic-embed-text` (Ollama): build new index alongside, then switch over
- [ ] **ADR:** embedding model / index version as a deployment unit
- [ ] **Shared:** write the Helix Cloud knowledge base (~12 runbooks, ~15 FAQs,
      ~8 past incidents) + `eval/questions.jsonl` (~30 labelled questions)

## Aditya Mahendru: Incident / Ticket Service (`feature/tickets`)

- [ ] Tickets table + state machine `OPEN → ACKED → RESOLVED`; illegal transition → 409
- [ ] `POST /tickets` with required `Idempotency-Key` (same key → same ticket)
- [ ] `GET /tickets/{id}`, `GET /tickets?status=`, `/ack`, `/resolve` per contract §2
- [ ] Every state change + its event in ONE transaction (`common.db.enqueue`), relay thread publishes
- [ ] Repeated ack/resolve → 200, no duplicate event
- [ ] Metric: `outbox_backlog`
- [ ] **ADR:** transactional outbox vs. writing to DB and queue separately (dual-write)
- [ ] **Shared:** finish the Grafana dashboard + `docs/RUNBOOK.md` (how to observe, break, recover)

## Raheel Vasudeva: On-Call & Escalation Service (`feature/oncall`)

- [ ] Roster table with `version`; `PUT /roster` uses optimistic concurrency (stale → 409)
- [ ] Consume `incident.opened` → create page for level 0; idempotent on `event_id`
- [ ] Ack timer: deadlines stored in DB (survive restart); background loop escalates on timeout
- [ ] `POST /pages/{id}/ack` → idempotent; calls Tickets `/ack` with timeout + retry
- [ ] Consume `incident.acked` / `incident.resolved` → cancel pending pages
- [ ] Metrics: `pages_sent_total`, `escalations_total`
- [ ] **ADR:** strong consistency on the roster vs. eventual
- [ ] **Shared:** `scripts/` failure-injection demos: kill Knowledge (breaker trips),
      stop RabbitMQ (outbox backlog grows, then drains), replay a duplicate event

## Together

- [ ] 5th ADR (e.g. why an async event bus for the incident lifecycle instead of synchronous calls)
- [ ] C4 diagrams (context + container) that match the code
- [ ] Report (≤ 8 pages), AI-disclosure appendix, contribution statement
- [ ] Week 10: bring 2–3 draft ADRs to the interim check
- [ ] Rehearse the 3-minute demo: ask → answer; ask gap → ticket → page → ack →
      resolve → ask again → answered; kill KB → degraded escalation
