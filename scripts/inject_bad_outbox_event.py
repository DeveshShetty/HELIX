"""Failure injection for the outbox relay: put an event that can never be routed in front of
a normal one, to show the relay no longer stops at the first failing event.

    docker compose exec -T tickets python - < scripts/inject_bad_outbox_event.py

The bad event uses a routing key no queue is bound to, so the (mandatory) publish is
returned as unroutable on every attempt; it must be retried and then parked, while the
good event created after it is published.
"""
import time

from common.db import connect, enqueue
from common.messaging import new_event

tag = int(time.time()) % 100000
bad = new_event("nobody.listens.here", f"T-BAD-{tag}", {"note": "outbox head-of-line demo"})
good = new_event("incident.reopened", f"T-HOL-{tag}", {"ticket_id": f"T-HOL-{tag}", "revision": 1,
                                                       "reopened_by": "demo", "reason": "outbox demo"})
conn = connect("/data/tickets.db")
conn.execute("BEGIN IMMEDIATE")
enqueue(conn, bad)                      # older: first in line
time.sleep(0.01)
enqueue(conn, good)
conn.execute("COMMIT")
conn.close()
print(f"bad={bad['event_id']} good={good['event_id']} good_ticket=T-HOL-{tag}")
