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

## Known limitations (honest list for the report)

- Repeated questions while the KB is down open one ticket each (5 pages for one problem).
- Lexical retrieval (hash-v1) cannot separate some near-misses (E1).
- Injection detection is pattern-based; it is one layer, not a guarantee.
- Daily budget is enforced per Chat instance (SQLite); several replicas would need a shared counter.
- All prices are notional; local models cost nothing to run.
