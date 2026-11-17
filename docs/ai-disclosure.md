# AI-disclosure log

Running log for the mandatory AI-disclosure appendix. Record every significant
AI assist: tool, prompt (summary), what was adopted / modified / rejected, why.

| Date | Member | Tool | Prompt (summary) | Adopted / modified / rejected and why |
|---|---|---|---|---|
| 2026-11-17 | Devesh Shetty | Claude Code (Claude Opus) | Scaffold Phase 0 from the Week 5 proposal and our Lab 2-4 code: shared lib, 4 service stubs, docker-compose, Prometheus/Grafana | Adopted the scaffold. Shared lib adapted from our labs (circuit breaker made thread-safe; outbox merged into `common/db.py`). Fixed a RabbitMQ startup failure (Erlang cookie permission on Windows) by adding a named volume. |
| 2026-11-17 | Devesh Shetty | Claude Code (Claude Opus) | Split work into four owners; draft service contracts (APIs, events, metrics), per-member task lists, KB seed format with 4 example Helix Cloud docs, and 7 example eval questions | Adopted the split and contracts as a starting point for team review. Examples are illustrative only: Liu writes the full knowledge base and eval set. |
