# Runbook: observe it, break it, watch it recover

## Start, stop, reset

```bash
docker compose up --build -d      # start everything (first build ~2 min)
docker compose ps                 # all services "healthy"
docker compose down               # stop, keep data
docker compose down -v            # stop and wipe all data (fresh knowledge base, no tickets)
```

## Where to look

| What | Where |
|---|---|
| Chat app | http://localhost:8001 |
| On-call console | http://localhost:8003 |
| Grafana dashboard | http://localhost:3000 (no login) |
| RabbitMQ queues | http://localhost:15672 (guest / guest) |
| Prometheus | http://localhost:9090 |
| Logs | `docker compose logs -f chat` (or `tickets`, `oncall`, `knowledge`) |
| Chat config, budget, breaker | http://localhost:8001/config |
| Knowledge index versions | http://localhost:8004/index |
| Ticket outbox | http://localhost:8002/outbox |

Useful log prefixes: `[decision]`, `[breaker:…]`, `[guard]`, `[escalation]`
(chat); `[tickets]`, `[outbox]` (tickets); `[ingest]`, `[index]` (knowledge);
`[page]`, `[escalate]` (on-call).

## Break it

Each scenario is scripted in `scripts/demo.py` (run with the stack up).

| Scenario | Command | Expected behaviour | Recovery | Dashboard panel |
|---|---|---|---|---|
| Knowledge Base down | `python scripts/demo.py kb-down` | Every question escalates with `degraded: true`, never a guess. After 3 failures the breaker opens; calls drop from ~2 s to ~100 ms | Start KB; after 15 s a half-open probe closes the breaker | Circuit breaker, Errors |
| Knowledge Base slow | `python scripts/demo.py kb-slow` | 2 s per-attempt deadline + 3 s retry budget cut slow calls; breaker opens | Remove fault; breaker closes | Latency p95, Circuit breaker |
| Ticket Service down during escalation | `python scripts/demo.py tickets-down` | User gets a reference number; escalation saved in chat's outbox | Worker delivers it when Tickets returns, same Idempotency-Key, no duplicate | Escalations waiting |
| RabbitMQ down | `python scripts/demo.py broker-down` | Tickets still created; events wait in the ticket outbox | Relay drains the outbox; On-Call pages | Ticket outbox backlog |
| On-Call restart mid-timer | `python scripts/demo.py oncall-restart` | Escalation timer survives the restart (deadlines are in the DB) | — | Paging |
| Duplicate events | `python scripts/demo.py duplicate` | Knowledge and On-Call log `duplicate … ignored` | — | Events consumed (`duplicate`) |
| Full loop | `python scripts/demo.py loop` (after `down -v`) | escalate → page → ack → resolve → same question answered → reopen → withdrawn | — | Answer / escalate |

Manual equivalents: `docker compose stop knowledge` / `start knowledge`, etc.
Shorter ack timeout for demos: `ACK_TIMEOUT_SECONDS=10 docker compose up -d oncall`.

## Common problems

| Symptom | Cause | Fix |
|---|---|---|
| `no configuration file provided` | Command run outside the repo folder | `cd` into the repo |
| RabbitMQ exits with `.erlang.cookie: eacces` | Windows volume permissions | Already handled by the named `rabbitmq_data` volume; `docker compose down -v` if it persists |
| Every question escalates with `uncalibrated_model` | Index rebuilt with a model that has no threshold | Add it to `CONFIDENCE_THRESHOLDS` after running `eval/run_eval.py` |
| Every question escalates with `daily_budget_exhausted` | Daily ceiling reached | Raise `DAILY_COST_CEILING_USD` or wait for 00:00 UTC |
| `LLM_MODE=ollama` answers are slow / `degraded` | llama3.2:1b on CPU; first call loads the model | Expected; answers fall back to extractive text if ungrounded |

## Switch embedding model (index versioning)

```bash
ollama pull nomic-embed-text
curl -X POST localhost:8004/index/rebuild -H "Content-Type: application/json" \
     -d '{"embedding_model": "nomic-embed-text"}'
curl localhost:8004/index        # watch BUILDING -> ACTIVE; the old version becomes RETIRED
```
Until a threshold for `nomic-embed-text` is calibrated and added to
`CONFIDENCE_THRESHOLDS`, Chat escalates everything (`uncalibrated_model`).
That is deliberate: an uncalibrated threshold would be a guess.
