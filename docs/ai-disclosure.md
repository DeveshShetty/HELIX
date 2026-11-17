# AI-disclosure log

Running log for the mandatory AI-disclosure appendix. Record every significant
AI assist: tool, prompt (summary), what was adopted / modified / rejected, why.

| Date | Member | Tool | Prompt (summary) | Adopted / modified / rejected and why |
|---|---|---|---|---|
| 2026-11-17 | Devesh Shetty | Claude Code (Claude Opus) | Scaffold Phase 0 from the Week 5 proposal and our Lab 2-4 code: shared lib, 4 service stubs, docker-compose, Prometheus/Grafana | Adopted the scaffold. Shared lib adapted from our labs (circuit breaker made thread-safe; outbox merged into `common/db.py`). Fixed a RabbitMQ startup failure (Erlang cookie permission on Windows) by adding a named volume. |
