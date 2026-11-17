"""Re-publish the most recent outbox event of a given type, to demonstrate
that consumers are idempotent (at-least-once delivery means duplicates happen).

    docker compose exec -T tickets python - < scripts/replay_last_event.py
    docker compose exec -T tickets python - incident.opened < scripts/replay_last_event.py
"""
import json
import sqlite3
import sys

from common.messaging import publish_event

event_type = sys.argv[1] if len(sys.argv) > 1 else "incident.resolved"
conn = sqlite3.connect("/data/tickets.db")
row = conn.execute(
    "SELECT payload FROM outbox WHERE json_extract(payload, '$.event_type') = ? "
    "ORDER BY created_at DESC LIMIT 1", (event_type,)).fetchone()
if not row:
    sys.exit(f"no {event_type} event in the outbox")
event = json.loads(row[0])
publish_event(event)
print(f"replayed {event_type} for {event['aggregate_id']} (event_id {event['event_id']})")
