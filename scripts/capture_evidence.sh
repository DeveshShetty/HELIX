#!/usr/bin/env bash
# Run the failure scenarios and save what happened to docs/evidence/<timestamp>/:
# demo output, the logs of every service for that window, and a metrics snapshot.
#   bash scripts/capture_evidence.sh            # all scenarios
#   bash scripts/capture_evidence.sh poison     # just some
# Needs the stack up (docker compose up -d) and python on PATH.
set -u
cd "$(dirname "$0")/.."
SCENARIOS=${*:-kb-down tickets-down broker-down oncall-restart duplicate poison}
OUT="docs/evidence/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT"
PY=$(command -v python3 || command -v python)

snapshot_metrics() {   # $1 = file
  for svc in chat:8001 tickets:8002 oncall:8003 knowledge:8004 summariser:8005; do
    echo "### ${svc%%:*}"
    curl -s "http://localhost:${svc##*:}/metrics/" | grep -E '^(dead_lettered_total|consumer_retries_total|outbox_parked_total|escalations_failed_total|escalations_pending|circuit_breaker_state|chat_decisions_total|events_consumed_total|ai_budget_|outbox_backlog|http_requests_total\{[^}]*status="5)'
  done > "$1"
}

snapshot_metrics "$OUT/metrics-before.txt"
for s in $SCENARIOS; do
  echo "== $s"
  "$PY" scripts/demo.py "$s" > "$OUT/$s.demo.txt" 2>&1
  echo "   exit $? -> $OUT/$s.demo.txt"
  sleep 3
  # --tail, not --since: Docker Desktop's VM clock can drift from the host clock
  docker compose logs --no-color --tail 150 chat tickets oncall knowledge summariser > "$OUT/$s.logs.txt" 2>&1
done
snapshot_metrics "$OUT/metrics-after.txt"
curl -s "http://localhost:9090/api/v1/alerts" > "$OUT/prometheus-alerts.json"
docker compose exec -T rabbitmq rabbitmqctl list_queues name messages --quiet > "$OUT/queues.txt" 2>&1
echo "saved to $OUT"
