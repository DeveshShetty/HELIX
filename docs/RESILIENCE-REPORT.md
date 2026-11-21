# HELIX: what it can and cannot handle

- Branch: `fix/major-audit-issues`, 2026-11-21.
- Basis: every claim below links to an entry in [EVIDENCE.md](EVIDENCE.md). If we did not
  run it, the report says so.
- Setup: all numbers come from one Windows laptop running Docker Desktop (3.8 GiB VM),
  usually with `LLM_MODE=simulated`.

## 1. System overview

A customer asks Chat a question. Chat searches Knowledge, then either answers from a help
article or escalates. An escalation becomes a ticket in Tickets, which announces it on
RabbitMQ. On-Call pages a person, who acks, talks to the customer and resolves the ticket.
The resolution goes back into Knowledge, and the Summariser turns the closed chat into a draft
help article for a person to approve.

```
customer -> Chat --HTTP--> Knowledge (search)
              |--HTTP--> Tickets --outbox--> RabbitMQ: incident.opened/acked/resolved/reopened
              |                                  |--> On-Call  (paging ladder)
              |                                  |--> Knowledge (index the resolution / withdraw it)
              |--outbox--> RabbitMQ: conversation.closed --> Summariser (draft article)
On-Call console --HTTP--> Tickets, Chat, Summariser
```

Each service owns its own SQLite file. RabbitMQ carries events between them. Prometheus and
Grafana watch everything.

**Chat (8001).** The customer-facing service. It refuses prompt injection and answers small
talk directly. For real questions it searches Knowledge. Every call has a hard deadline, a
retry budget and a circuit breaker. It answers only if the retrieval confidence clears the
threshold for the active index version; otherwise it escalates. A ticket request is saved
locally before Tickets is called, so a Tickets outage delays the escalation but never loses
it. Chat also holds the conversation log and hands live chats to agents.

**Tickets (8002).** The record of each incident: open, acked, resolved, reopened. Creating a
ticket requires an `Idempotency-Key`, so a retried request returns the same ticket. Each state
change and the event announcing it are written in one SQLite transaction (outbox). A relay
then publishes the event, and keeps accepting work while RabbitMQ is down.

**On-Call (8003).** Consumes incident events and pages a person from a roster. If nobody acks
within 30 s it moves to the next person. Deadlines live in its database, so a restart does not
lose the timer. The roster uses optimistic concurrency: a stale edit is rejected with 409.

**Knowledge (8004).** Stores help articles and resolved incidents, and serves `/search`. A
re-index builds a new index version next to the old one and switches only when it is complete.
It consumes `incident.resolved` (index the fix) and `incident.reopened` (withdraw it), applying
ticket revisions in order.

**Summariser (8005).** Consumes `conversation.closed` and drafts a help article from what the
agent said, with or without the LLM. Every AI step must be grounded in the agent's words, or
it is dropped.

**RabbitMQ.** One topic exchange, `support.events`, one durable queue per consumer, and a
dead-letter queue, `support.events.dlq`.

## 2. What the system can handle

| Scenario | Behaviour | Evidence | Recovery time |
|---|---|---|---|
| Knowledge down | Escalates, never guesses. After 3 failures the breaker opens and calls fail fast | [E5](EVIDENCE.md#e5-resilience-under-injected-failure), [E18](EVIDENCE.md#e18-evidence-capture-script-audit-fix-8) | Failures cost 2.4–3.2 s each, then about 0.5 s while open. Answers again on the first half-open probe after the 15 s open timeout |
| Knowledge slow (+5 s) | 2 s per-attempt deadline, then escalate; the breaker opens | E5 | Back to normal once the fault is removed and the breaker probes (15 s) |
| Knowledge erroring (50% 503) | The shared retry policy absorbed all 4 injected errors; 6/6 answered | [E20](EVIDENCE.md#e20-remaining-failure-scenarios-kb-erroring-tickets-hung-summariser-down-ollama-down-roster-409) | None needed. About 1 in 8 questions would still escalate at this error rate |
| Tickets down | Customer gets a reference; the escalation is stored and delivered later with the same key; no duplicate ticket | E18, [E12](EVIDENCE.md#e12-one-retry-policy-with-jitter-and-limits-outbox-no-longer-blocks-audit-fix-2) | Delivered within seconds of Tickets returning (backoff up to 60 s between tries). Gives up after 10 tries and alerts |
| Tickets hung (paused) | 3 s deadline, not a hang; escalation queued | E20 | Delivered 4 s after unpause |
| RabbitMQ down | Tickets still accepted into the outbox; consumers report 503 on `/health` | E18, [E15](EVIDENCE.md#e15-readiness-restart-policy-and-resource-limits-audit-fix-5) | Outbox drains in seconds once the broker is up. Consumers are healthy ~2 s after the broker, but the broker itself took 11–177 s to start on this laptop |
| On-Call restart mid-timer | The ladder continues from the database | E18 | Next person paged at the 30 s ack deadline |
| Duplicate events | Knowledge and On-Call ignore the replay (dedupe on `event_id`) | E18, [E16](EVIDENCE.md#e16-minimal-pytest-suite-audit-fix-6) | n/a |
| Summariser down | `conversation.closed` waits in its queue; the console shows 503 for summaries | E20 | Draft created within ~5 s of restart |
| Ollama down | Chat gives the extractive answer from the article (`degraded=true`); Summariser falls back to a rule-based draft | E20 (Chat), [E9](EVIDENCE.md#e9-summariser-service-few-shot-prompt-vs-instructions-only) (Summariser, earlier) | Immediate: 295 ms |
| Poison message | Retried 5 times at the back of the queue, then dead-lettered with the error; events behind it still processed; alert fires | [E11](EVIDENCE.md#e11-dead-letter-queue-for-poison-messages-audit-fix-1), E18, E16 | About 4–8 s to dead-letter. The message waits in the DLQ for a person |
| Unpublishable outbox event | Retried with its own counter, parked after 10 tries; later events are not blocked | E12, E16 | Later events unaffected |
| AI cost cap reached | Chat escalates, Summariser writes a rule-based draft, Knowledge stores without embedding; `ai_budget_denied_total` counts it | [E13](EVIDENCE.md#e13-one-daily-ai-budget-for-all-services-audit-fix-3), [E19](EVIDENCE.md#e19-load-test-on-post-chat) | At 00:00 UTC. Under load test, the default 200k-token day ran out in minutes |
| Idempotent retries | Same `Idempotency-Key` → same ticket, one event; a failed outbox write leaves no ticket and does not use up the key | E16, E5 | n/a |
| Concurrent roster edits | Second editor with a stale version gets 409; no half-applied roster | E20 | n/a |
| Clean start | Services become healthy only when ready; Knowledge answers 503 "starting" until its index is loaded | E15 | 46 s from `docker compose up` to all healthy |
| Index rebuild fails | The active index stays in service (the nomic build timed out once) | [E17](EVIDENCE.md#e17-threshold-keyed-by-index-version-v2-index-with-nomic-embed-text-audit-fix-7) | No outage |

One correlation id follows each request through Chat, Tickets and On-Call, so a single grep
finds it in every service's log
([E14](EVIDENCE.md#e14-correlation-ids-and-structured-logs-audit-fix-4)).

## 3. Consistency guarantees

Every message can arrive more than once, and every consumer is built so that the second copy does nothing. In detail:

- Transactional outbox (Tickets, Chat):
  - A state change and its event commit in one SQLite transaction. You never get a ticket
    without its `incident.opened`, or the reverse (E16, `test_ticket_rolled_back_when_outbox_write_fails`).
  - The relay publishes first and marks second. A crash in between republishes, and
    consumers dedupe.
  - A failing event is retried on its own and parked after 10 attempts. It no longer blocks
    the queue (E12).
- Idempotent consumers: Every consumer records processed `event_id`s in the same
  transaction as its effect. A redelivery or replay is ignored (E16, E18).
- Idempotent commands: Ticket creation needs an `Idempotency-Key`; Chat reuses one key
  per decision across all retries (E16, E5).
- Ordering:
  - RabbitMQ does not guarantee order across redeliveries, and a parked outbox event lets
    later ones overtake it.
  - Knowledge orders by ticket revision: an older `incident.resolved` is ignored, and a
    reopen leaves a tombstone so a late resolution cannot bring a withdrawn fix back (E16).
  - On-Call does not order `opened`/`acked`: both are revision 0. This is a known gap, left
    as it is.
- Poison messages: After 5 failed deliveries a message goes to `support.events.dlq` with
  its error. We traded "never lost" for "never blocks". The message is kept, but nothing replays it
  automatically, so someone has to go and look (E11).
- Roster: Optimistic concurrency with a version number; a stale write gets 409 (E20).
- What is not exactly-once:
  - The AI budget check is check-then-spend, so two services can overshoot by one call each (E13).
  - Repeated questions while Knowledge is down open one ticket each (known limitation list).

## 4. Load

Run with `scripts/load_test.py`, details in [E19](EVIDENCE.md#e19-load-test-on-post-chat).
Each simulated user sends a question, waits for the reply, then sends the next. 80% are KB
questions and 20% unknown ones. Simulated LLM, one laptop, Chat limited to 0.5 CPU.

| Users | Throughput | p50 | p95 | Error rate |
|---|---|---|---|---|
| 5 | 9.5 req/s | 323 ms | 1.7 s | 0% |
| 10 | 9.9 req/s | 587 ms | 3.3 s | 0% |
| **20 for 60 s** | **8.2 req/s** | **1.40 s** | **8.5 s** | **0.2%** |
| 40 | 6.4 req/s | 3.4 s | 19.1 s | 11.7% |
| 80 | 9.5 req/s | 6.2 s | 15.9 s | 6.3% |

**Where it degrades.**
- Throughput levels off at about 10 req/s from 10 users. Beyond that, more users only add
  waiting time.
- p95 goes past 5 s at 20 users.
- Errors start at 40 users. They are HTTP 500s from `sqlite3.OperationalError: database is
  locked` in Chat: each `/chat` does about five write transactions on one SQLite file, and
  with 40 at once the writers wait past the 10 s busy timeout.
- Memory was not the limit: Chat used 117–130 of 256 MiB.

We did not plan to test the budget here, but the first run did it anyway: with the default
daily AI budget, a 20-user load test used up the whole day's notional budget within minutes. After that every question escalated with `daily_budget_exhausted`. The cap
worked, but in production that would turn a traffic spike into a flood of tickets and pages.

## 5. What it cannot handle yet

Most of these are the known costs of running everything on one laptop with SQLite. The one that surprised us was the lock errors at 40 users: we expected memory to run out first, and it never came close.

| Gap | Impact | What would fix it |
|---|---|---|
| SQLite write contention in Chat | 500 errors from ~40 concurrent users; p95 > 5 s from 20 users (E19) | Fewer write transactions per request (one per `/chat`), a queue for writes, or Postgres |
| One instance per service, SQLite per service | No horizontal scaling; a lost Docker volume loses that service's data; no replication | Postgres (or similar) per service, stateless replicas behind a load balancer |
| AI budget is a shared file on one host | Works only when all services share one Docker host; can overshoot by one call per service | A small budget service or Redis counter with reserve-then-spend |
| Budget exhaustion becomes ticket flood | Every question escalates, opening a ticket and a page each (E19) | Per-customer rate limits; group escalations by question; a cheaper fallback before escalating |
| No latency or partition injection between services | We tested stop, pause, slow (whole service) and error injection, but not a slow or partitioned link between two services | toxiproxy or `tc netem` in the demo scripts |
| Disk full / DB locked / corrupt DB not demonstrated | Unknown behaviour; `/health` would report the DB as failing but nothing was forced | Fill a tmpfs volume in a demo; chaos tests on the DB file |
| Out-of-memory kill not demonstrated | Limits are set and `restart: unless-stopped` is on, but no container was pushed to OOM (E15) | Load a service past its limit and record the restart |
| No auth on admin endpoints | Anyone on the network can inject faults, rebuild the index, edit the roster or approve drafts; Grafana is anonymous Admin; RabbitMQ uses guest/guest | API keys or OIDC on admin routes; Grafana login; broker credentials from secrets |
| Linear vector search | Every query scans all chunks; fine for 44 chunks, not for thousands | A vector index (FAISS, pgvector) |
| nomic-embed-text v2 is slower and not better here | Query embeddings spike to 7.6 s on CPU; at the hash-v1 threshold it gives 21% wrong answers (E17) | Stay on hash-v1, or recalibrate with a larger question set and GPU-backed embeddings |
| Prompt-injection defence is pattern-based | A new phrasing can get through | A second, model-based classifier; stricter output checks |
| PII removal is pattern-based | Names and addresses are not removed | An NER-based redactor |
| DLQ has no replay tool | Dead-lettered messages must be moved by hand in the RabbitMQ UI | A small replay script with a dry-run mode |
| Outbox and `processed_events` never cleaned | Tables grow without limit | A periodic delete of old published/processed rows |
| No Kubernetes, no multi-host deployment | Restart and placement depend on one Docker host | Helm chart or a managed container service |
| Slow broker start | RabbitMQ took 11–177 s to start on this laptop; consumers are down for that long (E15) | More memory for the broker; quorum queues on a cluster |
| Docker Desktop clock drift | `docker compose logs --since` returned nothing; tooling had to use `--tail` (E18) | Use timestamps from the logs themselves; restart Docker Desktop after sleep |

## 6. Scores before and after

Same rubric as `AUDIT.md` (resilience and code quality, 0–100 per service). These are our own
estimates from the evidence, not marks, and we have probably been kinder to ourselves than an examiner would be.

| Service | Resilience before → after | Quality before → after | Why |
|---|---|---|---|
| chat | 78 → 84 | 62 → 72 | Shared retry with jitter; escalation worker gives up and alerts; correlation ids; DB readiness; index-versioned threshold. Held back by the SQLite lock errors from 40 users and the long `main.py` (left on purpose) |
| tickets | 74 → 83 | 68 → 77 | Outbox no longer blocks and parks bad events; tests for idempotency and outbox atomicity; readiness and limits |
| oncall | 63 → 75 | 58 → 66 | DLQ, jittered retries, broker readiness, roster 409 shown. No unit tests for the paging ladder; opened/acked ordering still open |
| knowledge | 65 → 79 | 64 → 73 | DLQ; 503 while starting; embeddings under the daily budget; tests for dedupe and revision order; v2 rebuild run and calibrated |
| summariser | 64 → 76 | 62 → 73 | DLQ; budget fallback; grounding tests (and a mutation check showing they catch a regression); broker readiness |

Shared library changes behind these numbers:
- `common/retry.py`, `log.py`, `health.py` and `budget.py`.
- The DLQ in `messaging.py`.
- 20 tests.

## 7. Remaining risks, ranked

1. Submission repository: The team repo (`helix-ds`) held only part of the system at
   audit time. If it is what gets marked, most of this work is not in it. The team decides.
2. No ADRs yet: The rubric weights the reasoning heavily, and it has to be the team's own.
3. SQLite write contention: It is the first thing to break under load (40 users).
4. Budget exhaustion turns into a paging storm: A spike uses up the day's AI budget, then
   every question opens a ticket and pages a person.
5. Open admin surface: Fault injection, rebuild, roster and approval have no auth;
   Grafana is anonymous Admin.
6. Messages parked in the DLQ or outbox need a person: The alerts fire, but there is no
   replay tool or runbook step beyond the RabbitMQ UI.
7. Pattern-based safety layers (injection, PII) miss new phrasings.
8. Outbox and `processed_events` grow without limit.
9. On-Call can see `opened` and `acked` out of order (both revision 0).
10. Single-host assumptions: the shared budget file, one broker, and Docker Desktop's
    slow broker start and clock drift.
