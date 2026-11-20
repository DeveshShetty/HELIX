"""Publish a conversation.closed event (a finished live-agent chat) so the Summariser drafts
an article for it. Used by the AI-budget check and the tests of the summariser path.

    docker compose exec -T chat python - < scripts/publish_closed_conversation.py
"""
import time

from common.messaging import new_event, publish_event

tag = int(time.time()) % 100000
cid, tid = f"c-budget-{tag}", f"T-BUDGET-{tag}"
msgs = [
    {"seq": 1, "role": "user", "text": "My invoices page shows the wrong currency after I moved to Ireland"},
    {"seq": 2, "role": "bot", "text": "I'm connecting you with a support agent."},
    {"seq": 3, "role": "agent", "text": "Open Billing > Settings and change the billing country to Ireland. "
                                        "Then click Save and reload the invoices page."},
    {"seq": 4, "role": "user", "text": "That fixed it, the invoices are in euro now, thanks"},
]
event = new_event("conversation.closed", cid, {"conversation_id": cid, "ticket_id": tid,
                                               "agent_id": "alice", "messages": msgs})
publish_event(event)
print(f"conversation={cid} ticket={tid} event_id={event['event_id']}")
