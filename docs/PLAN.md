# Build plan

| Phase | Scope | Status |
|---|---|---|
| 0 | Repo, shared lib, compose, Prometheus, Grafana | Done |
| 1 | Ticket Service (outbox) + Knowledge Base (index, idempotent re-indexing) | Done |
| 2 | Chat/RAG: resilience, confidence gate, LLM, cost, injection guard | Done |
| 3 | On-Call: roster, paging, durable timers, console | Done |
| 4 | Failure-injection demos + runbook | Done |
| 5 | Index versioning (blue/green rebuild), calibration curve, flip test | Done |
| 6 | Report, C4 diagrams, 5 ADRs, AI-disclosure appendix, contribution statement | **To do (Devesh)** |

See [TASKS.md](TASKS.md) for the detailed checklist and how each piece of
proposal feedback is addressed, and [EVIDENCE.md](EVIDENCE.md) for measured results.
