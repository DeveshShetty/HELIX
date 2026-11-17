# Status and remaining work

All four services are built by Devesh Shetty (the other members withdrew
from the project; the coordinator should be informed). Ownership in the
contribution statement must reflect who did what.

## Built and tested

- [x] Phase 0: skeleton, shared library, compose, Prometheus, Grafana
- [x] Chat / RAG: UI, decision path, circuit breaker, deadline + retry budget, injection guard,
      small talk, per-model thresholds, version stamps, token budget + daily ceiling, escalation outbox
- [x] Incident / Ticket: state machine, outbox, idempotency keys, revisions, reopen
- [x] Knowledge Base: hash-v1 + nomic-embed-text embedders, versioned index, blue/green rebuild,
      idempotent revision-ordered consumer, withdrawal on reopen, embedding cost
- [x] On-Call: roster with optimistic concurrency, durable ack timers, escalation chain, console
- [x] Helix Cloud knowledge base: 39 articles (12 runbooks, 17 FAQs, 8 incidents, 2 poisoned); browsable at :8004
- [x] 50-question test set, calibration curve, flip test (docs/EVIDENCE.md)
- [x] Failure-injection demos (scripts/demo.py) and runbook (docs/RUNBOOK.md)
- [x] Live hand-off to agents; stored conversations; personal-data removal; 'reusable answer' option
- [x] Summariser service (5th): async drafts from conversation.closed, grounding/injection/PII checks, approval queue
- [x] Dashboard: golden signals, AI cost/budget, resilience and consistency panels

## Proposal feedback (Dr Nag)

| # | Point | Where it is addressed |
|---|---|---|
| 1 | Define confidence from measurable signals | `compute_confidence` in services/chat/main.py; groundedness check in llm.py |
| 2 | Calibrate on a 40–50 question test set, plot the curve | eval/run_eval.py, eval/results/threshold_curve.svg, EVIDENCE E1 |
| 3 | Stamp chunks/answers with versions; threshold per model | chunk `model` column; response stamps; `CONFIDENCE_THRESHOLDS`; EVIDENCE E2 |
| 4 | Measure decision flips over 20 runs | eval/flip_test.py, EVIDENCE E3 |
| 5 | Feedback loop as injection vector; source tags; no tools | staff-written summary only; `origin`/`author` on chunks; EVIDENCE E7 |
| 6 | Token budget per query + daily ceiling | `MAX_PROMPT_TOKENS`, `DAILY_*_CEILING`; EVIDENCE E6 |
| 7 | Escalation never lost; timers survive restart | chat escalation outbox; pages.deadline in DB; EVIDENCE E5 |
| 8 | Reopened incident withdraws the answer | `incident.reopened`, revision ordering in Knowledge |

## Remaining (cannot be delegated to AI under module policy)

- [ ] **Tell the coordinator** that the other members withdrew, and name the owner of each service
- [ ] **Week 10 ADR check:** draft the answer-or-escalate ADR (what confidence means, the
      threshold, the rejected alternative, the fact that would change your mind) using EVIDENCE E1–E4
- [ ] Five ADRs in the five-band form (docs/adr/TEMPLATE.md). Candidates:
      answer-or-escalate threshold; index versioning / blue-green rebuild; transactional outbox;
      roster consistency; async event bus vs synchronous calls; escalation outbox
- [ ] Report (≤ 8 pages): C4 context + container diagrams, trade-off analyses, consistency and
      resilience sections citing EVIDENCE, AI section, cost/ops view. Proofread (feedback asked for it)
- [ ] AI-disclosure appendix (from docs/ai-disclosure.md) and contribution statement
- [ ] Rehearse the 3-minute demo and be able to explain every service (viva)
