"""Dead-letter after N failures, outbox parking, circuit breaker, retry limits."""
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from common import messaging, retry, topology
from common.circuit_breaker import CircuitBreaker, CircuitState
from common.db import OUTBOX_DDL, PARKED, connect, enqueue, migrate_outbox, relay_once


# ------------------------------------------------------------------ dead-letter queue

class FakeChannel:
    def __init__(self):
        self.published, self.acked = [], []

    def basic_publish(self, exchange, routing_key, body, properties):
        self.published.append(SimpleNamespace(routing_key=routing_key, body=body, properties=properties))

    def basic_ack(self, delivery_tag):
        self.acked.append(delivery_tag)


def test_poison_message_is_dead_lettered_after_max_attempts():
    ch, body = FakeChannel(), b'{"event_type": "incident.resolved"}'
    method = SimpleNamespace(routing_key="incident.resolved", exchange="support.events", delivery_tag=1)
    props = SimpleNamespace(headers=None, content_type="application/json", message_id="m-1", correlation_id="c-1")
    outcomes = []
    for _ in range(messaging.MAX_DELIVERY_ATTEMPTS):
        outcomes.append(messaging.handle_failure(ch, "q.test", method, props, body, ValueError("bad"),
                                                 sleep=lambda s: None))
        props = ch.published[-1].properties                # the next delivery is the re-published copy

    assert outcomes == ["retried"] * (messaging.MAX_DELIVERY_ATTEMPTS - 1) + ["dead-lettered"]
    retries = [p.routing_key for p in ch.published[:-1]]
    assert retries == ["q.test"] * (messaging.MAX_DELIVERY_ATTEMPTS - 1)   # back of its own queue
    dlq = ch.published[-1]
    assert dlq.routing_key == topology.DEAD_LETTER_QUEUE
    assert dlq.properties.headers["x-original-queue"] == "q.test"
    assert dlq.properties.headers["x-last-error"] == "ValueError: bad"
    assert isinstance(dlq.properties.headers["x-dead-lettered-at"], int)   # AMQP can't carry floats
    assert dlq.properties.message_id == "m-1"
    assert len(ch.acked) == messaging.MAX_DELIVERY_ATTEMPTS               # every copy acked once


# ------------------------------------------------------------------ outbox relay

@pytest.fixture
def outbox(tmp_path):
    conn = connect(str(tmp_path / "svc.db"))
    conn.executescript(OUTBOX_DDL)
    migrate_outbox(conn)
    yield conn
    conn.close()


def _event(n):
    return {"event_id": f"e{n}", "event_type": "incident.opened", "aggregate_id": f"T-{n}", "correlation_id": "c"}


def test_failing_outbox_event_does_not_block_later_ones_and_is_parked(outbox, monkeypatch):
    monkeypatch.setattr("common.db.OUTBOX_MAX_ATTEMPTS", 3)
    enqueue(outbox, _event(1))
    time.sleep(0.01)
    enqueue(outbox, _event(2))
    sent = []

    def publish(ev):
        if ev["event_id"] == "e1":
            raise RuntimeError("unroutable")
        sent.append(ev["event_id"])

    for _ in range(3):
        relay_once(outbox, "test.db", publish=publish)
        outbox.execute("UPDATE outbox SET next_attempt_at=0")      # skip the backoff wait
    assert sent == ["e2"]                                          # not blocked behind e1
    rows = dict(outbox.execute("SELECT event_id, published FROM outbox").fetchall())
    assert rows == {"e1": PARKED, "e2": 1}


def test_broker_outage_does_not_charge_events(outbox):
    import pika
    enqueue(outbox, _event(3))

    def down(ev):
        raise pika.exceptions.AMQPConnectionError("broker down")

    with pytest.raises(pika.exceptions.AMQPConnectionError):
        relay_once(outbox, "test.db", publish=down)
    assert outbox.execute("SELECT attempts, published FROM outbox").fetchone()[:] == (0, 0)


# ------------------------------------------------------------------ circuit breaker

def test_circuit_breaker_opens_then_closes_after_successful_probe():
    cb = CircuitBreaker("test", failure_threshold=3, window_size=5, open_timeout=0.05)
    for _ in range(3):
        assert cb.allow_request()
        cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert not cb.allow_request()                       # fail fast while open
    time.sleep(0.06)
    assert cb.allow_request()                           # one probe in HALF_OPEN
    assert cb.state == CircuitState.HALF_OPEN
    assert not cb.allow_request()                       # only one probe at a time
    cb.record_success()
    assert cb.state == CircuitState.CLOSED


def test_failed_probe_reopens_the_circuit():
    cb = CircuitBreaker("test2", failure_threshold=1, window_size=5, open_timeout=0.01)
    cb.record_failure()
    time.sleep(0.02)
    assert cb.allow_request()
    cb.record_failure()
    assert cb.state == CircuitState.OPEN


# ------------------------------------------------------------------ shared retry helper

def test_retry_stops_at_max_attempts_and_backoff_has_jitter():
    calls, sleeps = [], []

    def always_fails():
        calls.append(1)
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        retry.call(always_fails, attempts=4, base=0.1, cap=1.0, sleep=sleeps.append)
    assert len(calls) == 4 and len(sleeps) == 3
    samples = {round(retry.backoff(3, 1.0, 30.0), 6) for _ in range(20)}
    assert len(samples) > 1 and all(2.0 <= s <= 4.0 for s in samples)   # equal jitter: [d/2, d]


def test_retry_does_not_retry_non_retryable_errors():
    calls = []

    def bad_request():
        calls.append(1)
        raise ValueError("4xx")

    with pytest.raises(ValueError):
        retry.call(bad_request, attempts=5, retryable=lambda e: not isinstance(e, ValueError),
                   sleep=lambda s: None)
    assert len(calls) == 1
