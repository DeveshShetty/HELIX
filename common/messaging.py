"""RabbitMQ helpers shared by all services (adapted from Lab 2).

Delivery semantics: at-least-once. Publishers use publisher confirms; consumers
ack only after the handler succeeds, so every handler MUST be idempotent
(dedupe on event_id).
"""
import json
import os
import time
import uuid
from typing import Callable

import pika

from common import topology

EXCHANGE = os.getenv("RABBIT_EXCHANGE", "support.events")
RABBIT_HOST = os.getenv("RABBIT_HOST", "localhost")
RABBIT_PORT = int(os.getenv("RABBIT_PORT", "5672"))
RABBIT_USER = os.getenv("RABBIT_USER", "guest")
RABBIT_PASSWORD = os.getenv("RABBIT_PASSWORD", "guest")


def connection_parameters() -> pika.ConnectionParameters:
    return pika.ConnectionParameters(
        host=RABBIT_HOST,
        port=RABBIT_PORT,
        credentials=pika.PlainCredentials(RABBIT_USER, RABBIT_PASSWORD),
        heartbeat=30,
        blocked_connection_timeout=15,
        connection_attempts=3,
        retry_delay=2,
    )


def new_event(event_type: str, aggregate_id: str, data: dict | None = None,
              correlation_id: str | None = None) -> dict:
    """Build an event envelope. event_id is the idempotency key for consumers."""
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,          # e.g. "incident.resolved"
        "aggregate_id": aggregate_id,      # e.g. the ticket id
        "correlation_id": correlation_id or aggregate_id,
        "occurred_at": time.time(),
        "data": data or {},
    }


_topology_declared = False


def publish_event(event: dict) -> None:
    """Publish a persistent event; raises if the broker does not confirm it."""
    global _topology_declared
    connection = pika.BlockingConnection(connection_parameters())
    try:
        channel = connection.channel()
        if not _topology_declared:
            topology.declare(channel, EXCHANGE)
            _topology_declared = True
        channel.confirm_delivery()
        channel.basic_publish(
            exchange=EXCHANGE,
            routing_key=event["event_type"],
            body=json.dumps(event).encode("utf-8"),
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,
                message_id=event["event_id"],
                correlation_id=event["correlation_id"],
            ),
            mandatory=True,
        )
    finally:
        connection.close()


def consume_forever(queue_name: str, routing_keys: list[str],
                    handler: Callable[[dict], None]) -> None:
    """Consume with automatic reconnect. A handler exception nacks + requeues."""
    while True:
        connection = None
        try:
            connection = pika.BlockingConnection(connection_parameters())
            channel = connection.channel()
            topology.declare(channel, EXCHANGE)
            channel.queue_declare(queue=queue_name, durable=True)
            for key in routing_keys:
                channel.queue_bind(exchange=EXCHANGE, queue=queue_name, routing_key=key)
            channel.basic_qos(prefetch_count=1)

            def callback(ch, method, properties, body):
                try:
                    event = json.loads(body.decode("utf-8"))
                    if properties.message_id:
                        event.setdefault("event_id", properties.message_id)
                    handler(event)
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                except Exception as exc:
                    print(f"[{queue_name}] handler error: {exc}; requeueing", flush=True)
                    time.sleep(1)  # crude backoff so a poison message doesn't spin the CPU
                    ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

            channel.basic_consume(queue=queue_name, on_message_callback=callback)
            print(f"[rabbit] consuming {queue_name} <- {routing_keys}", flush=True)
            channel.start_consuming()
        except Exception as exc:
            print(f"[rabbit] {queue_name} disconnected: {exc}; retrying in 3s", flush=True)
            time.sleep(3)
        finally:
            try:
                if connection and connection.is_open:
                    connection.close()
            except Exception:
                pass
