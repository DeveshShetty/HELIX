"""Publish one malformed event followed by one valid event (failure injection: poison message).

Run inside any service container (they all have pika and common/):
    docker compose exec -T tickets python - < scripts/publish_poison.py

The malformed incident.resolved has no summary/resolution, so Knowledge's handler
raises on every attempt and the message must end up in support.events.dlq.
The valid incident.reopened behind it must still be processed.
Prints the two ticket ids so the caller can look for them in the logs.
"""
import time

from common.messaging import new_event, publish_event

tag = int(time.time()) % 100000
poison_id, ok_id = f"T-POISON-{tag}", f"T-DLQOK-{tag}"

poison = new_event("incident.resolved", poison_id, {"ticket_id": poison_id, "revision": 1})   # no summary, no fix
publish_event(poison)
valid = new_event("incident.reopened", ok_id, {"ticket_id": ok_id, "revision": 1, "reopened_by": "demo", "reason": "dlq demo"})
publish_event(valid)
print(f"poison={poison_id} event_id={poison['event_id']}")
print(f"valid={ok_id} event_id={valid['event_id']}")
