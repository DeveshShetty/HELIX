"""Knowledge consumer: duplicate events are ignored, stale revisions are ignored."""
import uuid

from conftest import load_service, wait_until

kb = load_service("knowledge")
wait_until(lambda: kb._startup["ready"])               # seed + index load in the background


def resolved(ticket_id: str, revision: int, event_id: str | None = None) -> dict:
    return {"event_id": event_id or str(uuid.uuid4()), "event_type": "incident.resolved",
            "aggregate_id": ticket_id,
            "data": {"ticket_id": ticket_id, "revision": revision, "resolved_by": "alice",
                     "summary": f"Fix for {ticket_id} revision {revision}",
                     "resolution": f"Open Settings and choose option {revision}."}}


def reopened(ticket_id: str, revision: int) -> dict:
    return {"event_id": str(uuid.uuid4()), "event_type": "incident.reopened", "aggregate_id": ticket_id,
            "data": {"ticket_id": ticket_id, "revision": revision, "reason": "fix was wrong"}}


def doc_text(ticket_id: str) -> str | None:
    conn = kb.connect(kb.DB_PATH)
    try:
        row = conn.execute("SELECT text FROM documents WHERE doc_id=?", (f"inc-{ticket_id}",)).fetchone()
    finally:
        conn.close()
    return row["text"] if row else None


def test_duplicate_event_is_ignored():
    ev = resolved("T-DUP", 1)
    assert kb.index_document  # sanity: module loaded
    kb.handle_incident_event(ev)
    before = kb.REINDEX.labels("incident", "ok")._value.get()
    kb.handle_incident_event(dict(ev))                  # same event_id: redelivery
    assert kb.REINDEX.labels("incident", "ok")._value.get() == before
    assert "option 1" in doc_text("T-DUP")


def test_older_revision_is_ignored():
    kb.handle_incident_event(resolved("T-ORDER", 2))
    kb.handle_incident_event(resolved("T-ORDER", 1))    # arrives late
    assert "option 2" in doc_text("T-ORDER")


def test_reopen_tombstone_blocks_late_resolution():
    kb.handle_incident_event(resolved("T-TOMB", 1))
    kb.handle_incident_event(reopened("T-TOMB", 2))
    assert doc_text("T-TOMB") is None
    kb.handle_incident_event(resolved("T-TOMB", 1))     # a late copy must not resurrect it
    assert doc_text("T-TOMB") is None


def test_malformed_resolution_raises_so_it_can_be_dead_lettered():
    bad = resolved("T-BAD", 1)
    bad["data"].pop("resolution")
    try:
        kb.handle_incident_event(bad)
    except ValueError:
        return
    raise AssertionError("malformed event was accepted")
