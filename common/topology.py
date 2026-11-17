"""The message topology: which durable queue receives which events.

Single source of truth, declared (idempotently) by EVERY service at startup,
publishers included. Why: events are published as `mandatory`, so an event
with no bound queue is rejected and stays in the publisher's outbox. If queues
only appeared when their consumer first started, one missing consumer would
block the outbox relay for everyone (head-of-line blocking: the relay
publishes in order, so a stuck incident.opened held back incident.resolved).
With the topology declared up front, events for a consumer that is down wait
in its durable queue and are delivered when it comes back.
"""
QUEUES: dict[str, list[str]] = {
    "oncall.incident-lifecycle": ["incident.opened", "incident.acked", "incident.resolved", "incident.reopened"],
    "knowledge.incident-lifecycle": ["incident.resolved", "incident.reopened"],
    "summariser.conversations": ["conversation.closed"],
}


def declare(channel, exchange: str) -> None:
    channel.exchange_declare(exchange=exchange, exchange_type="topic", durable=True)
    for queue, keys in QUEUES.items():
        channel.queue_declare(queue=queue, durable=True)
        for key in keys:
            channel.queue_bind(exchange=exchange, queue=queue, routing_key=key)
