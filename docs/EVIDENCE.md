# Evidence log

Measured results for the report and ADRs. Each entry says how to reproduce it.
These are facts; the interpretation and the decisions belong in the ADRs.

## E1. Threshold calibration (proposal feedback point 2)

`python eval/run_eval.py` on 50 labelled questions (30 covered, 8 near-miss,
5 gap, 4 injection, 3 small talk). Output: `eval/results/threshold_curve.csv`, `.svg`.

hash-v1, index rebuilt over all 39 seed documents (`hash-v1-20261117T184243`), 45 retrieval questions:

| threshold | answer rate | wrong-answer rate | false escalations |
|---|---|---|---|
| 0.30 | 100% | 9% | 0 |
| 0.35 | 100% | 9% | 0 |
| **0.40** | **100%** | **7%** | **0** |
| 0.45 | 94% | 7% | 2 |
| 0.55 | 88% | 2% | 4 |
| 0.60 | 81% | 2% | 6 |
| 0.75 | 50% | 0% | 16 |

47/50 correct at 0.40. The 3 errors are near-misses the lexical model cannot
separate: "export invoices as **CSV**" (0.73, matched the invoice-download FAQ),
"rate limit **per API key**" (0.58), "dashboard blank in **Firefox**" (0.53,
matched the Safari incident). No threshold removes them without losing
correct answers: below 2% wrong answers costs 12–19% of answerable questions.

## E2. The threshold belongs to an index version, not just a model (feedback point 3)

Same questions, same model (hash-v1), two index versions:

| | index built at 4 docs, 35 added incrementally | index rebuilt over 39 docs |
|---|---|---|
| answer rate at 0.40 | 94% | 100% |
| "status page" confidence | 0.38 | 0.42 |

hash-v1 stores corpus statistics (IDF) with the index, so incremental
additions keep stale weights. Earlier, moving from the mock TF-IDF index to
hash-v1 changed the best threshold from 0.45 to 0.30–0.40.

## E3. Decision flips (feedback point 4)

`python eval/flip_test.py`. Full output: `eval/results/flip_test.txt`.

| LLM | questions (closest to threshold) | runs each | decision flips | distinct answer wordings |
|---|---|---|---|---|
| simulated | 6 (confidence 0.30–0.53, threshold 0.40) | 20 | 0 | 1 |
| llama3.2:1b | 4 (0.38–0.48) | 10 | 0 | up to 3 |

The generated text varies with Ollama; the decision does not, because it is
taken from retrieval scores before generation.

## E4. llama3.2:1b output quality

With an "if unsure say I don't know" instruction, llama3.2:1b replied "I don't
know" to questions its document answered (2FA reset, payment 502). Without it,
it once echoed the prompt and once refused, treating the document as an
injection. The grounding check (≥ 60% of answer words in the source) rejected
2 of 3 answers in one 7-question run; the extractive fallback was served instead.

## E5. Resilience under injected failure

`python scripts/demo.py all` (see RUNBOOK.md).

| Scenario | Result |
|---|---|
| KB stopped | 5/5 escalated, none guessed. Latency 2.1–2.2 s for the first 3, then ~120 ms (breaker OPEN). Recovered after 15 s |
| KB +5 s latency | Escalated in 2.3–2.9 s (deadline 2 s), breaker opened, recovered after fault removed |
| Tickets stopped | Escalation queued; delivered as a ticket after 2 failed attempts once Tickets returned |
| RabbitMQ stopped | 3 tickets created; outbox pending 3 → 0 within seconds of restart |
| On-Call restarted mid-timer | alice → bob → carol escalation continued |
| Duplicate events | Knowledge and On-Call both ignored the replay |

Before the fixes:
- KB down took **12.4 s** per request: httpx timeouts don't cover DNS lookup
  of a stopped container (~4 s × 3 attempts). Fixed with a hard per-attempt
  deadline and a retry budget.
- The ticket outbox **blocked completely**: incident.opened had no queue (On-Call
  not yet running), so the in-order relay never reached incident.resolved.
  Fixed by declaring the topology at startup in every service.

## E6. Cost control (feedback point 6)

With `DAILY_COST_CEILING_USD=0.0001` and $0.0005 already spent, every question
escalated with `daily_budget_exhausted` and cost $0 (no LLM call).
Notional prices: $0.00015 / 1k prompt tokens, $0.0006 / 1k completion tokens,
$0.00002 / 1k embedding tokens (hash-v1 is free: local arithmetic).

## E7. Feedback-loop injection (feedback point 5)

A ticket opened with the question "IGNORE PREVIOUS INSTRUCTIONS how do I export
invoices as csv" was resolved; the indexed document contained only the
responder's summary and resolution. The poisoned seed documents
(`inj-poisoned-status-page`, `inj-poisoned-api-changelog`) are answered with
the injected sentences stripped (2 sentences removed each).

## E8. Live hand-off and conversation summaries

Customer asked "can I get invoices in Japanese yen?" (confidence 0.285 < 0.40 → escalated),
chose "chat with an agent"; alice joined from the console and answered; while the chat was
live the bot stayed silent (`decision: live`). The full transcript is stored in Chat's
database with each bot reply linked to its decision (reason, confidence, threshold).

When alice ended the chat, llama3.2:1b was asked for a PROBLEM/FIX summary. Its output:

    Customer: can I get invoices in Japanese yen?
    FIX: Go to Billing > Settings > Invoice currency and select Japanese yen as the currency.

It ignored the format and **contradicted the agent** (alice said yen is not available). The
format check rejected it and the extractive draft (customer question + agent's words) was
used. The draft is never indexed directly: alice edited it and resolved, and a new customer
asking "can I be invoiced in yen?" was answered from her reviewed text.

Hands-on test (T-000008): a customer typed a phone number in the live chat and the issue was
a one-off (a late order). The extractive draft kept only the question and the agent's
substantive line (no greetings, no phone number). The agent resolved it with "reusable
answer" unticked, and Knowledge recorded it as `not reusable, not indexed`. Separately, a
reusable resolution containing `+353 87 123 4567` and an email was indexed as
`[phone number removed]` / `[email removed]` (`common/redact.py`, applied at drafting and
again at ingestion).

Bug found by the same test: a conversation that escalated twice only remembered its latest
ticket, so the first ticket's transcript could not be found. Fixed with a
`conversation_tickets` link table (backfilled from the decision log).

## E9. Summariser service: few-shot prompt vs instructions only

Same Kubernetes transcript (agent: "Kubernetes deployments are supported on the Team and
Enterprise plans: go to Apps > New app > Deploy from Kubernetes manifest and upload your
YAML file"), llama3.2:1b, temperature 0.1, 3 runs each:

| prompt | format followed | content |
|---|---|---|
| instructions only | 0/3 | "The agent suggested the customer contact support team" (wrong) |
| + one worked example (few-shot) | 3/3 | FIX = the agent's steps; grounding 100% each run |

The few-shot FIX dropped "Team and Enterprise plans only": faithful but incomplete, one
reason a person still approves every draft. Ending the chat now returns in ~50 ms
(drafting moved off the request path); the draft appeared ~8 s later.

## E10. Summary drafted about the wrong problem (found by a hands-on test)

One chat held two questions: an Android file problem the bot answered from the knowledge
base, then an AWS deployment problem that was escalated to alice. The summariser was given
the whole transcript, including the bot's answer. llama3.2:1b titled the article "Android
app file opening issues", copied a step from the bot's answer ("check the permissions for
location and files"), and invented a cause. The grounding check passed it (55%) because the
agent's echo of the customer ("I am facing issues with aws") counted as agent text.

Fixes, verified by replaying the same AI output through the new checks:
- only the escalated question and the customer/agent messages after it are summarised;
  the bot's answers are never shown to the summariser
- grounding is computed per step against the agent's advice sentences only (greetings and
  echoes excluded); unsupported steps are dropped (1 of 3 here)
- the cause must reach 70% overlap with the agent's advice, else "Not confirmed"
- the title/symptoms must overlap the escalated question, else the rule-based title is used
Result: "Issues with the AWS deployment", two steps from alice's advice, cause not confirmed.

Two more bugs found while reviewing: a customer queued for an agent stayed in
`WAITING_AGENT` forever if nobody joined (the bot ignored them from then on), fixed with
"Stop waiting" and automatic release when the ticket is resolved; and approving a summary
for an already-resolved ticket reported success while nothing was indexed, now a 409.

## E11. Dead-letter queue for poison messages (audit fix 1)

`python scripts/demo.py poison` publishes a malformed `incident.resolved` (no summary or
fix) and, right behind it, a valid `incident.reopened` (`scripts/publish_poison.py`).
Run on 2026-11-21, `MAX_DELIVERY_ATTEMPTS=5`:

```
knowledge  handler error on e4ae5752-… (attempt 1/5): ValueError: incident.resolved needs summary and resolution; retrying later
knowledge  [ingest] T-DLQOK-99668 reopened (rev 1): withdrawn            <- valid event processed between retries
knowledge  handler error on e4ae5752-… (attempt 2/5) … (attempt 3/5) … (attempt 4/5)
knowledge  DEAD-LETTERED message e4ae5752-… after 5 attempts: ValueError: incident.resolved needs summary and resolution
DLQ depth (rabbitmqctl list_queues): 1 -> 2
dead_lettered_total{queue="knowledge.incident-lifecycle"} = 2      (Prometheus)
alert MessagesDeadLettered queue=knowledge.incident-lifecycle state=firing
```

Before the fix, the same message was nacked and requeued at the head of the queue forever,
blocking everything behind it (prefetch 1). Now a failing message goes to the back of its own
queue with an `x-helix-attempts` header and a backoff with jitter, and after the last attempt
it moves to `support.events.dlq` with `x-last-error`, `x-original-queue`,
`x-original-routing-key` and the original message id.

Bug found while verifying: the first version put a float timestamp in the dead-letter
headers. AMQP header tables can't encode floats, so pika dropped the connection, the message
was redelivered, and it looped at attempt 5 (the first DLQ message above, `fc8194a8…`, was
stuck like that). Fixed with an integer timestamp; the stuck message was then dead-lettered
on the next delivery.

Dashboard: panels "Poison messages: retries and dead-lettered" and "Dead-lettered (total)";
Prometheus rule `MessagesDeadLettered` in `monitoring/prometheus/rules.yml`.
Not verified: a dead-lettered message cannot be replayed from the DLQ with a script yet; it
has to be moved by hand in the RabbitMQ UI.

## E12. One retry policy with jitter and limits; outbox no longer blocks (audit fix 2)

`common/retry.py` (backoff with "equal jitter", max attempts, overall deadline, optional hard
per-attempt deadline) replaces the three hand-written loops (Chat clients, On-Call `_call`,
Summariser `_tickets_request`; the last two now share `common/http.request_json`). It is
also used by the consumer reconnect, the outbox relay and the Chat escalation worker. Run on
2026-11-21 with demo limits `OUTBOX_MAX_ATTEMPTS=3`, `ESCALATION_MAX_ATTEMPTS=3`.

Jitter (`backoff(attempt, base=1, cap=30)`, three samples per attempt):
`{1: [0.95, 0.79, 0.77], 2: [1.16, 1.19, 1.78], 3: [3.74, 3.88, 2.83], 6: [15.02, 27.89, 21.22]}`

**Outbox head-of-line blocking fixed** (`scripts/inject_bad_outbox_event.py` puts an
unroutable event in front of a valid one):
```
tickets   [outbox] event 020e9f16-… failed (attempt 1/3): UnroutableError: 1 unroutable message(s) returned
tickets   [outbox] event 020e9f16-… failed (attempt 2/3): UnroutableError: …
tickets   [outbox] PARKED event 020e9f16-… after 3 attempts: UnroutableError: …
knowledge [ingest] T-HOL-99920 reopened (rev 1): withdrawn          <- the event behind it was delivered
outbox rows by state: [(-1 parked, 1), (1 published, 72)]
outbox_parked_total{db="tickets.db"} 1.0
```

**Escalation worker stops instead of retrying forever** (Tickets stopped, one escalating
question):
```
chat  [escalation] ERROR gave up on d-2534f62dc2e8 after 3 attempts: AttemptTimeout('no response within 3.0s'); customer request needs manual follow-up
escalations_failed_total 1.0;  decision d-2534f62dc2e8 escalation.status = FAILED
alert EscalationsGivenUp = firing
```

**A broker outage does not park events** (`demo.py broker-down`, still with the limit at 3):
3 tickets created while RabbitMQ was stopped, outbox pending 3 → 0 after restart, parked count
unchanged (only the bad event above). `AMQPConnectionError` stops the relay pass without
charging an attempt to any event. Consumer reconnects now back off:
`reconnect attempt 1 … attempt 2 in 1.3s … attempt 3 in 3.2s` (previously a fixed 3 s).

Regression: `demo.py kb-down` unchanged (2.2–2.3 s per failure, then ~250 ms with the breaker
open).

Trade-off to state in the report: skipping a failed outbox event means later events can be
published before it. Knowledge already orders by ticket revision; On-Call does not for
opened/acked (both revision 0, listed as a known limitation).

## E13. One daily AI budget for all services (audit fix 3)

Before: only Chat's answers counted against the daily ceiling. Knowledge embeddings and
Summariser drafts were metered but never stopped. Now `common/budget.py` keeps one ledger
(SQLite on the `ai_budget` volume, mounted in chat, knowledge and summariser). Every model call
is recorded there, and every service checks the same day total before it spends.

Run on 2026-11-21, `LLM_MODE=ollama` (llama3.2:1b), `DAILY_TOKEN_CEILING=700` (tiny on purpose):

```
1. conversation.closed -> summariser S-7 drafted (llama3.2:1b (summary-v3-article))
   budget: tokens_today 427, by_service {summariser: 427 tokens, $0.000095}
2. chat "how do I reset my password"               -> answer / grounded
   chat "how do I export my data"                  -> escalate / daily_budget_exhausted
   chat "how do I enable two-factor authentication" -> escalate / daily_budget_exhausted
   chat "how do I cancel my subscription"          -> escalate / daily_budget_exhausted
   budget: tokens_today 702, by_service {chat: 275, summariser: 427}
3. conversation.closed after the cap:
   summariser  [budget] summariser/summary denied: today 702 tokens, $0.000182 (ceiling 700 tokens / $0.5)
   summariser  S-8 drafted (rule-based (AI draft not used)); checks failed: ['ai_budget']
metrics: ai_budget_denied_total{service="chat",purpose="answer"} 3
         ai_budget_denied_total{service="summariser",purpose="summary"} 1
         ai_budget_tokens_today 702 (same value on :8001 and :8005: one shared counter)
```

So the Summariser's spend used up Chat's room: Chat answered once and then escalated.

Knowledge: the only paid embedder is nomic-embed-text, and it is **not installed** on this
machine (Ollama has llama3.2:1b only). `scripts/check_kb_budget.py` runs the real
`embed_texts`/`index_document` code against a throw-away DB, with a stand-in embedder priced
and metered as nomic-embed-text:

```
[budget] knowledge/index denied: today 728 tokens, $0.000183 (ceiling 700 tokens / $0.5)
index_document: stored_unindexed              <- document saved, no chunks; next rebuild indexes it
[budget] knowledge/query denied: …
query embedding: skipped -> daily AI budget used up; query embedding skipped   (/search -> 503)
document stored: 1 | chunks indexed: 0
ai_budget_denied_total{service=knowledge,purpose=index} 1, {…,purpose=query} 1
with the ceiling raised: index_document: indexed, chunks indexed: 1
```

The live KB on hash-v1 (local and free, not counted) kept answering `/search` over the cap.

Limits, not verified or not handled:
- A real nomic-embed-text run against the cap was not done (the model is not installed).
- Check-then-spend has no reservation, so two services can each pass the check just before
  the ceiling and overshoot it by one call.
- The ledger is a file on one Docker host. It does not work across machines.
- Chat's per-decision cost stamps (decisions table) are unchanged; the ceiling now reads the
  shared ledger.

Grafana panel "Shared daily AI budget: tokens used and calls skipped".

## E14. Correlation ids and structured logs (audit fix 4)

`common/log.py` is set up once per service: `instrument()` installs it, so no service needed
its own setup. All 45 `print()` calls in `common/` and `services/` are now logger calls with
levels: INFO for normal flow, WARNING for retries, degraded paths and duplicates, ERROR for
dead-lettered, parked, given up and failed builds. Every line looks like:
`<UTC time> <LEVEL> service=<svc> corr=<id> <logger> | <message>`.

How the id travels:
- HTTP: the `X-Request-Id` header is read on the way in (or a new `req-…` id is made). It is
  returned on the response and sent on outbound calls (`common/http.py`, `chat/clients.py`;
  the retry pool copies the context into its worker threads).
- Broker: `new_event()` stamps the current id as `correlation_id`, and `consume_forever()`
  restores it before the handler runs, so events a handler publishes carry it on.
- Background work: the outbox relay sets the id of the event it is publishing. A late Chat
  escalation keeps the customer's request id in its stored payload.

Run on 2026-11-21 (`LLM_MODE=simulated`). One escalated chat, grep for its id across all
services:

```
POST /chat -> X-Request-Id: req-08eb81ae8d32 (also body.request_id), escalate/low_confidence, T-000056
tickets 18:50:27.337Z INFO  service=tickets corr=req-08eb81ae8d32 tickets | [tickets] T-000056 OPEN (incident.opened queued in outbox)
chat    18:50:27.476Z INFO  service=chat    corr=req-08eb81ae8d32 chat    | [decision] d-4b51ef910f7a escalate/low_confidence conf=0.081 thr=0.4 emb=hash-v1 ticket=T-000056
oncall  18:50:27.683Z INFO  service=oncall  corr=req-08eb81ae8d32 oncall  | [page] PAGING alice for T-000056 (level 0, ack within 30s) P-9790
```

The same id survives a delayed escalation (Tickets stopped, then started again):

```
chat    18:50:47.885Z … corr=req-3fc3ffb202c9 chat    | [decision] d-1cb7b8d3e4c3 escalate/low_confidence … ticket=None
tickets 18:50:57.070Z … corr=req-3fc3ffb202c9 tickets | [tickets] T-000057 OPEN (incident.opened queued in outbox)
chat    18:50:57.142Z … corr=req-3fc3ffb202c9 chat    | [escalation] d-1cb7b8d3e4c3 delivered late as T-000057
oncall  18:50:57.700Z … corr=req-3fc3ffb202c9 oncall  | [page] PAGING alice for T-000057 (level 0, ack within 30s) P-9835
```

A caller's own id is kept: `GET /health` with `X-Request-Id: demo-trace-42` returns
`demo-trace-42`.

Limits:
- Later steps of an incident (ack, resolve, reopen) are new HTTP requests from the console,
  so they get new ids. The ticket id is the join key across that lifecycle.
- Uvicorn's own access-log lines keep uvicorn's format, without the corr field.
- Lines are key=value text, not JSON. There is no log shipping or search: `docker compose logs | grep`.

## Known limitations (honest list for the report)

- Repeated questions while the KB is down open one ticket each (5 pages for one problem).
- Lexical retrieval (hash-v1) cannot separate some near-misses (E1).
- Injection detection is pattern-based; it is one layer, not a guarantee.
- Daily budget is enforced per Chat instance (SQLite); several replicas would need a shared counter.
- All prices are notional; local models cost nothing to run.
- Personal-data removal is pattern-based (phone, email, card); names and addresses are not detected.
- Live chat uses polling (1.5 s), not WebSockets; fine for a demo, not for thousands of chats.
- The customer is not notified when a ticket they did not chat about is resolved.
