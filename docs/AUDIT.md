# Self-audit against the marking rubric

Strict, evidence-based review of the code as an examiner would read it. **Findings, not
fixes**: nothing was changed while auditing.

- **Audited:** `DeveshShetty/HELIX`, main `7d46764` (2026-11-20). All `file:line`
  references are relative to the repository root.
- **Also checked:** the team repository `aditya-mahendru/helix-ds` (see mismatch 2).
- **Rubric:** architecture and trade-offs (35), implementation (25), resilience and
  consistency (15), report and ADRs (15), AI use and disclosure (10).

Status key: **Done** / **Partial** / **Missing** / n/a.

---

## 1. Services

`docker-compose.yml` defines five application services plus RabbitMQ, Prometheus and
Grafana. There is no Kubernetes deployment.

| Service | Port | Responsibility | Datastore | Communication |
|---|---|---|---|---|
| chat | 8001 | Answer / escalate / refuse; live hand-off; customer UI | SQLite `chat.db`: decisions, conversations, messages, pending_escalations, outbox | Sync out: knowledge `/search`, tickets `/tickets`, Ollama. Async out: `conversation.closed` (outbox) |
| tickets | 8002 | Incident system of record; state machine | SQLite `tickets.db`: tickets, idempotency_keys, outbox, processed_events | Sync in only. Async out: `incident.*` (outbox) |
| oncall | 8003 | Roster, paging, escalation timers, engineer console | SQLite `oncall.db`: roster, pages, incidents projection | Sync out: tickets, chat, summariser. Async in: `incident.*` |
| knowledge | 8004 | Versioned vector index, ingestion, article browser | SQLite `knowledge.db`: documents, chunks, indexes, incident_revisions | Sync in: `/search`. Sync out: Ollama embeddings. Async in: `incident.resolved` / `incident.reopened` |
| summariser | 8005 | Draft articles from closed chats; approval queue | SQLite `summariser.db`: drafts | Async in: `conversation.closed`. Sync out: tickets, Ollama |
| rabbitmq | 5672 | Topic exchange `support.events` | durable queues | — |
| prometheus / grafana | 9090 / 3000 | Metrics and dashboard | — | scrape `/metrics/` |

---

## 2. Audit per service

### 2.0 Shared library (`common/`): affects every service

| Check | Status | Evidence | Risk |
|---|---|---|---|
| Broker timeouts | Done | `common/messaging.py:29-32` (heartbeat 30 s, blocked 15 s, 3 attempts) | — |
| DB timeouts | Done | `common/db.py:31,34` (timeout 10 s, busy_timeout 10 s) | — |
| Dead-letter queue / poison messages | **Missing** | `common/messaging.py:99-102`: every handler error is nacked with `requeue=True` after 1 s, forever | **High**: one malformed event blocks its queue permanently (prefetch 1) |
| Consumer reconnect | Partial | `common/messaging.py:81,107-109`: infinite loop, fixed 3 s, no backoff or jitter | Low |
| Outbox relay retry | Partial | `common/db.py:69-81`: infinite, fixed 0.5 s, stops at the first failing event | Medium: head-of-line blocking |
| Outbox / processed_events cleanup | **Missing** | no `DELETE FROM outbox` or `processed_events` anywhere | Medium: unbounded growth |
| Jitter on retries | **Missing** | `services/chat/clients.py:64`, `services/oncall/main.py:194`, `services/summariser/main.py:487`, `services/chat/store.py:159` | Synchronised retry storms after recovery |
| Correlation / trace IDs | **Missing in practice** | `correlation_id` set (`common/messaging.py:43,70`) but never logged; no request ID on HTTP calls; no tracing | High for diagnosis |
| Logging | Partial | `print()` throughout; no levels, not structured | Medium |
| Metrics | Done | `common/observability.py:26` on every service, plus per-service counters | — |
| Tests | **Missing** | no `tests/`, no pytest; only `eval/` scripts and `scripts/demo.py` | **High** for criterion 2 |
| Resource limits / restart policy | **Missing** | `docker-compose.yml`: no `deploy.resources`, `mem_limit` or `restart:` | High in practice (the 7.4 GB laptop ran out of memory twice) |
| Auth on admin endpoints | **Missing** | `services/knowledge/main.py:535` (fault injection), `:514` (rebuild); `services/oncall/main.py:252` (roster); draft approval; `docker-compose.yml:96-97` Grafana anonymous Admin | Medium (acceptable for a demo if stated) |
| Secrets | Partial | config via env; RabbitMQ `guest/guest` default (`docker-compose.yml:14`) | Low (development only) |
| Duplicated code | Partial | three retry helpers: `services/chat/clients.py:50`, `services/oncall/main.py:181`, `services/summariser/main.py:475`; STOPWORDS and `_stem` copied in `services/chat/main.py` and `services/knowledge/embedders.py` | Medium: inconsistent behaviour |

### 2.1 chat (`services/chat/`)

| Check | Status | Evidence | Risk |
|---|---|---|---|
| Timeouts | Done | KB 2 s with a hard deadline that also covers DNS (`clients.py:17,59,79`); Tickets 3 s (`clients.py:18,96`); Ollama 30 s (`llm.py:20,92`) | — |
| Retries + backoff | Partial | budgeted retries (`clients.py:50-69`), no jitter; no retry on the LLM call (`llm.py:92`) | Low |
| Circuit breaker / fallback | Done | `clients.py:22-88`; KB down → degraded escalation (`main.py:278-283`); LLM failure → extractive answer (`llm.py:109`) | — |
| Idempotency | Partial | ticket creation keyed `chat-<decision_id>` (`main.py:194`, `clients.py:92-100`); `POST /chat` has no idempotency key (`main.py:232`); repeated questions open duplicate tickets | Medium |
| Outbox | Done | `conversation.closed` in the same transaction (`conversations.py:127-147`); escalation outbox (`store.py`, `main.py:176-191`) | — |
| Escalation worker | Partial | `store.py:153-161`: backoff capped at 30 s, no maximum attempts, no alert | Medium: retries forever |
| Health / readiness | Partial | `main.py:528` returns 200 without checking DB or KB | Low |
| Input validation | Done | `ChatIn` 1–2000 characters (`main.py:219`); empty / too long / missing body → 422 (verified) | — |
| AI cost caps | Done | per-query token cap `main.py:48,328`; daily token and cost ceiling `main.py:49-50,271` | Covers chat only (see mismatch 4) |
| AI injection defence | Done | user input `main.py:256`; retrieved documents `main.py:300`; output `main.py:335`; patterns `common/guard.py:32-49` | Pattern-based only (stated) |
| AI versioning | Done | `PROMPT_VERSION` (`llm.py:40`); threshold per embedding model (`main.py:46,291-293`); every decision stamped | Not keyed by index version (mismatch 6) |
| AI non-determinism | Done | decision taken from retrieval before generation (`main.py:134`); flip tests 0/4 and 0/6 | — |
| AI output validation | Done | grounding ≥ 0.6, otherwise extractive (`llm.py:41,103-106`) | — |
| Code structure | Partial | `main.py` is 535 lines mixing routing, policy, small-talk copy and hand-off | Harder to explain at the viva |

**Resilience 78/100 · Quality 62/100**

### 2.2 tickets (`services/tickets/main.py`)

| Check | Status | Evidence | Risk |
|---|---|---|---|
| Timeouts | Done | DB only (`common/db.py:31`); no outbound HTTP | — |
| Idempotent POST | Done | `Idempotency-Key` required (`:145`); check and insert atomic under `BEGIN IMMEDIATE` (`:149-166`) | Keys never expire |
| Idempotent transitions | Done | repeat transition is a no-op with no event (`_transition`, `:206`) | — |
| Outbox | Done | state change and event in one transaction (`:167`, `:225`); relay started `:96` | Head-of-line blocking via `common/db.py` |
| Revisions / ordering | Done | revision bumped on resolve and reopen (`:206`) | — |
| Health | Partial | `:283` does not expose outbox lag (metric only, `:87-92`) | Low |
| Validation | Done | severity pattern, summary 5–200 characters; 422 / 404 verified | — |
| Error handling | Done | rollback on exception (`:171-175`, `:231-235`) | — |
| Tests | Missing | — | — |

**Resilience 74/100 · Quality 68/100**

### 2.3 oncall (`services/oncall/main.py`)

| Check | Status | Evidence | Risk |
|---|---|---|---|
| Timeouts | Done | `_call` 3 s (`:186`); summariser 10 s (`:375`) | — |
| Retries | Partial | `:184-194`: up to 3 attempts with backoff, no jitter; several calls `attempts=1` (`:375`, console, close) | Low |
| Circuit breaker | **Missing** | no breaker on Tickets, Chat or Summariser calls; console polls every 2 s | Medium |
| Fallback | Partial | console falls back to the local projection when Tickets is down (`:394-424`) | — |
| Idempotent consumer | Done | `:114-152` (dedupe `:122`, mark `:145`); idempotent ack (`ack_page :282-305`) | — |
| Durable timers | Done | deadline stored (`:98`); loop `:158-177`; verified across a restart | — |
| Strong-consistency roster | Done | optimistic version check (`put_roster :252`); stale write → 409 (verified) | — |
| Swallowed exceptions | Partial | `release_waiting_customer :316-325` (`except HTTPException: pass`); console `:399,412,421` | A failed customer release is invisible |
| Out-of-order events | Partial | projection upserts by revision, but `opened` and `acked` both carry revision 0 | Low |
| DLQ | Missing | via `common/messaging.py` | High |
| Auth | Missing | `PUT /roster` open (`:251`) | Medium |

**Resilience 63/100 · Quality 58/100**

### 2.4 knowledge (`services/knowledge/`)

| Check | Status | Evidence | Risk |
|---|---|---|---|
| Timeouts | Partial | Ollama embedding 60 s (`embedders.py:91`), no retry | Medium during a v2 rebuild |
| Idempotent consumer | Done | `index_document :253-293`, `withdraw_incident :295-320`; dedupe plus revision ordering (`:266-281`, `:300-314`) | — |
| Build-alongside-then-switch rebuild | Done in code | `build_index :198-250` with catch-up and atomic switch | v2 path never executed (mismatch 5) |
| Fault injection | Done | `/admin/fault :535` | Unauthenticated |
| Readiness | Partial | `health :542-545` returns 200 with `"status":"starting"`; Docker only checks the code | Traffic before the index is ready |
| Startup | Partial | `load_seed()` at import time (`:409`); index build blocks server start | Slow start looks unhealthy |
| PII | Done | `redact` on ingestion (`handle_incident_event :355`) | Patterns only |
| Cost | Partial | embedding tokens and cost counted (`:89,142`), no cap | Point 6 only half done |
| Swallowed errors | Partial | `_safe_build :531-532` `pass` (failure is logged and recorded) | Low |
| Search scaling | Partial | linear in-memory scan | Fine at 44 documents; state it |

**Resilience 65/100 · Quality 64/100**

### 2.5 summariser (`services/summariser/main.py`)

| Check | Status | Evidence | Risk |
|---|---|---|---|
| Timeouts | Done | Ollama 60 s (`:45,242`); Tickets 3 s (`:480`) | — |
| Retries | Partial | Tickets up to 3 attempts (`:475-495`), no jitter; LLM no retry, falls back to rules | — |
| Idempotent consumer | Done | `handle_conversation_closed :348-385` (checked twice, `:354`, `:366`) | — |
| Output validation | Done | per-step grounding, cause, topic, injection, PII (`draft :273-345`; thresholds `:47,51`) | — |
| Approval guard | Done | 409 when the ticket is already resolved (`approve :498`) | — |
| Cost cap | **Missing** | `summariser_llm_*` metrics only; not counted against any ceiling | Point 6 |
| Prompt versioning | Done | `SUMMARY_PROMPT_VERSION :76`, stamped in `method` | — |
| Long LLM call inside the consumer | Partial | up to 60 s per message with prefetch 1, so the queue stalls behind one slow draft | Medium with Ollama |
| DLQ | Missing | via `common/messaging.py` | High |

**Resilience 64/100 · Quality 62/100**

### Scores

| Service | Resilience | Quality |
|---|---|---|
| chat | 78 | 62 |
| tickets | 74 | 68 |
| oncall | 63 | 58 |
| knowledge | 65 | 64 |
| summariser | 64 | 62 |

---

## 3. Report-vs-code mismatches

Claims in the README, docs or diagrams that the code or the evidence does not support:

1. **No ADRs and no report.** `docs/adr/` contains only `TEMPLATE.md`; there is no report
   file. The rubric requires five ADRs in the five-band form.
2. **`helix-ds` cannot start with one command.** It holds only `common/`,
   `services/knowledge`, `services/summariser`: no `docker-compose.yml`, `monitoring/`,
   `chat`, `tickets`, `oncall` or README. Submitted as it is, criterion 2 fails.
3. **"Every call has a timeout and retries"** (`docs/ARCHITECTURE.md`): LLM calls make
   one attempt (`services/chat/llm.py:92`, `services/summariser/main.py:242`,
   `services/knowledge/embedders.py:91`), and some On-Call calls use `attempts=1`
   (`services/oncall/main.py:375`).
4. **"LLM and embedding cost against the daily ceiling"** (`docs/architecture.svg`;
   `docs/TASKS.md` marks feedback point 6 as addressed): the ceiling
   (`services/chat/main.py:271`) sums only Chat's decisions. Embedding cost
   (`services/knowledge/main.py:142`) and Summariser cost are measured but never capped.
5. **Index versioning with nomic-embed-text "Done"** (`docs/PLAN.md` phase 5, README,
   diagrams): the code path exists but has never been run; there is no evidence entry and
   the model is not pulled.
6. **Threshold "per model" marked addressed**, but `docs/EVIDENCE.md` E2 shows the
   threshold belongs to the index version. The code keys by model only
   (`services/chat/main.py:46,291`).
7. **"Dashboard evidences it"**: there are no Grafana screenshots or exports in `docs/`.
8. **EVIDENCE E1 (47/50) cannot be reproduced as written.** The set is now 54 questions
   and the live knowledge base has learned three "gap" answers, so a re-run gives 48/54
   with different failures. The README does not say to start from `docker compose down -v`.
9. **No dead-letter queue is mentioned anywhere**, and poison events loop forever
   (`common/messaging.py:99-102`). Not a false claim, but an obvious examiner question.
10. **AI disclosure is not in the module citation format.** `docs/ai-disclosure.md` is a
    summary table without verbatim prompts or session references.

---

## 4. Failure-injection plan

| # | Target | Command | Expected behaviour | Proof (log / metric / panel) | Demonstrable now |
|---|---|---|---|---|---|
| 1 | Knowledge down | `docker compose stop knowledge` or `python scripts/demo.py kb-down` | escalate, `degraded`; breaker opens after 3 failures; ~2 s then ~100 ms | `[breaker:chat->knowledge] CLOSED -> OPEN`; `circuit_breaker_state`; panel "Circuit breaker" | Yes |
| 2 | Knowledge slow | `curl -X POST localhost:8004/admin/fault -H "Content-Type: application/json" -d '{"latency_ms":5000}'` or `demo.py kb-slow` | 2 s deadline; breaker opens | "Latency p95", "Circuit breaker" | Yes |
| 3 | Knowledge errors | `.../admin/fault -d '{"error_rate":0.5}'` | retries on 503; escalates when the budget is spent | `[kb] attempt ... failed`; "Errors (5xx)" | Yes (not scripted) |
| 4 | Tickets down | `docker compose stop tickets` or `demo.py tickets-down` | customer gets a reference; delivered later with the same key | `[escalation] ... delivered late`; `escalations_pending` | Yes |
| 5 | Tickets hung | `docker compose pause tickets` / `docker compose unpause tickets` | 3 s deadline; escalation outbox; console uses local projection | as #4 | Yes (not scripted) |
| 6 | RabbitMQ down | `docker compose stop rabbitmq` or `demo.py broker-down` | tickets accepted; backlog grows then drains | `outbox_backlog`; "Ticket outbox backlog" | Yes |
| 7 | On-Call restart mid-timer | `docker compose restart oncall` or `demo.py oncall-restart` | escalation continues | `[escalate]` log; "Paging" panel | Yes |
| 8 | Duplicate events | `demo.py duplicate` | consumers ignore the replay | `events_consumed_total{outcome="duplicate"}` | Yes |
| 9 | Summariser down | `docker compose stop summariser`, then end a chat | `conversation.closed` waits in its queue; console shows "Summariser unreachable"; drafted after restart | RabbitMQ queue depth; "Summaries waiting" | Yes (not scripted) |
| 10 | Chat down | `docker compose stop chat` | customer UI shows "can't reach"; console live-chat panel errors; tickets unaffected | 5xx on chat | Partial: nothing graceful to show |
| 11 | Ollama down / slow | stop the Ollama app with `LLM_MODE=ollama` | extractive answers and rule-based drafts | `[llm] ollama failed`; degraded badge | Partial: possible, not scripted; slowness cannot be injected |
| 12 | Poison message | publish a malformed `incident.resolved` | today: requeued forever, queue blocked | `handler error ... requeueing` repeating | **No: demonstrates a gap** |
| 13 | Latency / partition between two services | toxiproxy or `tc netem` | — | — | **No tooling** |
| 14 | DB lock / disk full | — | — | — | **Not demonstrable** |
| 15 | Dashboard evidence for 1–8 | screenshots during each demo | — | — | **Not captured yet** |

---

## 5. Summary

### Top 10 fixes, ranked by rubric points per hour

| # | Fix | Criteria | Effort |
|---|---|---|---|
| 1 | Make `helix-ds` complete and runnable (compose, monitoring, all services, docs), or submit HELIX | 2 | 0.5 h |
| 2 | Write the five ADRs. **The reasoning must be the team's own: the brief does not allow delegating it to AI.** `docs/EVIDENCE.md` E1–E10 provides the facts | 1, 4 | 6–8 h |
| 3 | Capture dashboard evidence: run demos 1, 4, 6, 7, 8 and add Grafana screenshots to `docs/EVIDENCE.md` | 3 | 1 h |
| 4 | Dead-letter queue: count deliveries, dead-letter after N attempts, alert metric | 3 | 1.5 h |
| 5 | Count embedding and Summariser cost in the daily ceiling (shared counter or per-service caps) | 1, 5 | 1.5 h |
| 6 | Pytest suite: idempotency, outbox, breaker, revision ordering, Summariser grounding | 2 | 3 h |
| 7 | One shared retry helper in `common/` with jitter and a maximum; cap the escalation worker and alert | 2, 3 | 1 h |
| 8 | Correlation IDs: propagate `X-Request-Id` / `correlation_id` and log it on every line | 2 | 2 h |
| 9 | Readiness and limits: Knowledge returns 503 while starting; health checks DB and broker; compose `mem_limit` and `restart: unless-stopped` | 2, 3 | 1 h |
| 10 | Run and record the v2 index rebuild (nomic-embed-text); key the threshold by index version | 1 | 2 h |

### Estimated band per criterion

| Criterion | If submitted today | After fixes 1–5 |
|---|---|---|
| 1. Architecture and trade-offs (35) | Adequate: strong design, but no ADRs, so it reads as "defaulted rather than decided" | Excellent, if the ADRs are genuinely the team's |
| 2. Implementation (25) | Good (HELIX); Adequate or below (`helix-ds` as it stands) | Good–Excellent (tests and correlation IDs for Excellent) |
| 3. Resilience and consistency (15) | Good: demos work, outbox and idempotency verified; no DLQ, no dashboard evidence | Excellent |
| 4. Report and ADRs (15) | Below Adequate: no report, no ADRs (diagrams exist) | Good–Excellent |
| 5. AI use and disclosure (10) | Good: strong guardrails; cost caps incomplete; disclosure not in citation format | Excellent |

The two largest risks are mismatch 2 (`helix-ds` does not run) and mismatch 1 (no ADRs,
no report). Both are cheap compared with the marks they carry.
