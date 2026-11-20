"""RabbitMQ helpers shared by all services (adapted from Lab 2).

Delivery semantics: at-least-once. Publishers use publisher confirms; consumers
ack only after the handler succeeds, so every handler MUST be idempotent
(dedupe on event_id).

Poison messages: a failing message is NOT requeued at the head of its queue
(with prefetch 1 that blocked every event behind it forever). It is re-published
to the BACK of its own queue with an attempt counter in its headers, after a
backoff, and the original is acked. After MAX_DELIVERY_ATTEMPTS it is published
to the dead-letter queue with the error and its original routing, and counted in
dead_lettered_total{queue}.
"""
import json
import os
import random
import time
import uuid
from typing import Callable

import pika
from prometheus_client import Counter

from common import topology

EXCHANGE = os.getenv("RABBIT_EXCHANGE", "support.events")
MAX_DELIVERY_ATTEMPTS = int(os.getenv("MAX_DELIVERY_ATTEMPTS", "5"))
CONSUMER_RETRY_BASE = float(os.getenv("CONSUMER_RETRY_BASE", "0.5"))
CONSUMER_RETRY_CAP = float(os.getenv("CONSUMER_RETRY_CAP", "5"))
ATTEMPT_HEADER = "x-helix-attempts"

DEAD_LETTERED = Counter("dead_lettered_total", "Messages moved to the dead-letter queue", ["queue"])
CONSUMER_RETRIES = Counter("consumer_retries_total", "Failed deliveries sent back for another attempt", ["queue"])
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


def handle_failure(ch, queue_name: str, method, properties, body: bytes, exc: Exception,
                   sleep: Callable[[float], None] = time.sleep) -> str:
    """Decide what happens to a message whose handler raised. Returns "retried" or "dead-lettered".

    The channel must be in confirm mode, so the re-publish is confirmed before the
    original is acked (a crash in between gives a duplicate, never a loss).
    """
    headers = dict(properties.headers or {})
    attempts = int(headers.get(ATTEMPT_HEADER, 1))           # attempts so far, including this one
    headers.setdefault("x-original-routing-key", method.routing_key)
    headers.setdefault("x-original-exchange", method.exchange)
    headers["x-last-error"] = f"{type(exc).__name__}: {exc}"[:500]
    props = dict(content_type=properties.content_type or "application/json", delivery_mode=2,
                 message_id=properties.message_id, correlation_id=properties.correlation_id)

    if attempts >= MAX_DELIVERY_ATTEMPTS:
        # AMQP header tables cannot carry Python floats (pika raises and drops the
        # connection, so the message was redelivered forever): integer epoch seconds.
        headers.update({"x-original-queue": queue_name, "x-dead-lettered-at": int(time.time()),
                        ATTEMPT_HEADER: attempts})
        ch.basic_publish(exchange="", routing_key=topology.DEAD_LETTER_QUEUE, body=body,
                         properties=pika.BasicProperties(headers=headers, **props))
        ch.basic_ack(delivery_tag=method.delivery_tag)
        DEAD_LETTERED.labels(queue_name).inc()
        print(f"[{queue_name}] DEAD-LETTERED message {properties.message_id} after {attempts} attempts: "
              f"{headers['x-last-error']}", flush=True)
        return "dead-lettered"

    delay = min(CONSUMER_RETRY_CAP, CONSUMER_RETRY_BASE * 2 ** (attempts - 1))
    sleep(delay / 2 + random.uniform(0, delay / 2))           # backoff with jitter
    headers[ATTEMPT_HEADER] = attempts + 1
    ch.basic_publish(exchange="", routing_key=queue_name, body=body,   # back of its own queue
                     properties=pika.BasicProperties(headers=headers, **props))
    ch.basic_ack(delivery_tag=method.delivery_tag)
    CONSUMER_RETRIES.labels(queue_name).inc()
    print(f"[{queue_name}] handler error on {properties.message_id} (attempt {attempts}/{MAX_DELIVERY_ATTEMPTS}): "
          f"{headers['x-last-error']}; retrying later", flush=True)
    return "retried"


def consume_forever(queue_name: str, routing_keys: list[str],
                    handler: Callable[[dict], None]) -> None:
    """Consume with automatic reconnect. A failing message is retried, then dead-lettered."""
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
            channel.confirm_delivery()          # re-publishes in handle_failure are confirmed

            def callback(ch, method, properties, body):
                try:
                    event = json.loads(body.decode("utf-8"))
                    if properties.message_id:
                        event.setdefault("event_id", properties.message_id)
                    handler(event)
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                except Exception as exc:
                    handle_failure(ch, queue_name, method, properties, body, exc)

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
