"""Ticket Service: idempotency key and transactional outbox."""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import load_service

tickets = load_service("tickets")
client = TestClient(tickets.app)
BODY = {"title": "Cannot export data", "question": "how do I export my data", "severity": "medium"}


def outbox_rows(ticket_id: str) -> list[dict]:
    conn = tickets.connect(tickets.DB_PATH)
    try:
        rows = conn.execute("SELECT payload FROM outbox").fetchall()
    finally:
        conn.close()
    return [e for e in (json.loads(r["payload"]) for r in rows) if e["aggregate_id"] == ticket_id]


def test_same_idempotency_key_creates_one_ticket():
    first = client.post("/tickets", json=BODY, headers={"Idempotency-Key": "chat-d-test-1"})
    again = client.post("/tickets", json=BODY, headers={"Idempotency-Key": "chat-d-test-1"})
    assert first.status_code == 201
    assert again.status_code == 200                     # a retry is answered, not repeated
    assert again.json()["ticket_id"] == first.json()["ticket_id"]
    assert len(outbox_rows(first.json()["ticket_id"])) == 1     # and announced once


def test_different_keys_create_different_tickets():
    a = client.post("/tickets", json=BODY, headers={"Idempotency-Key": "chat-d-test-2"}).json()
    b = client.post("/tickets", json=BODY, headers={"Idempotency-Key": "chat-d-test-3"}).json()
    assert a["ticket_id"] != b["ticket_id"]


def test_missing_idempotency_key_is_rejected():
    assert client.post("/tickets", json=BODY).status_code == 422


def test_outbox_event_written_in_same_transaction():
    t = client.post("/tickets", json=BODY, headers={"Idempotency-Key": "chat-d-test-4"}).json()
    events = outbox_rows(t["ticket_id"])
    assert [e["event_type"] for e in events] == ["incident.opened"]


def test_ticket_rolled_back_when_outbox_write_fails(monkeypatch):
    def broken_enqueue(conn, event):
        raise RuntimeError("disk full while writing the outbox")

    monkeypatch.setattr(tickets, "enqueue", broken_enqueue)
    before = client.get("/tickets").json()
    with pytest.raises(RuntimeError):
        client.post("/tickets", json=BODY, headers={"Idempotency-Key": "chat-d-test-5"})
    after = client.get("/tickets").json()
    assert len(after) == len(before)                    # no ticket without its event
    monkeypatch.undo()
    retry = client.post("/tickets", json=BODY, headers={"Idempotency-Key": "chat-d-test-5"})
    assert retry.status_code == 201                     # the key was not consumed by the failure
