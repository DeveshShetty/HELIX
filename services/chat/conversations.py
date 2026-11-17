"""Conversations and live hand-off to a human agent (stored in Chat's database).

Every message is stored: customer -> Helix bot, the bot's replies (linked to
the decision record, so "why it got stuck" is kept: reason, confidence,
threshold), and the live customer <-> agent conversation after a hand-off.

Status of a conversation:
  BOT            the Helix bot answers
  WAITING_AGENT  the customer asked for a person; the bot stays quiet
  LIVE           an agent has joined; messages go between customer and agent
  CLOSED         the agent ended the chat; the customer can talk to the bot again

Closing a chat writes a `conversation.closed` event (carrying the transcript) to
Chat's OUTBOX in the same transaction as the status change. The Summariser
service consumes it, drafts a summary in the background and queues it for an
agent to approve. Chat itself no longer summarises: slow LLM work stays out of
the request path, and the summary pipeline can fail without affecting chat.
"""
import time

from common.db import OUTBOX_DDL, connect, enqueue
from common.messaging import new_event

DDL = """
CREATE TABLE IF NOT EXISTS conversations (
  conversation_id TEXT PRIMARY KEY,
  status          TEXT NOT NULL DEFAULT 'BOT',
  ticket_id       TEXT,
  agent_id        TEXT,
  created_at      REAL NOT NULL,
  updated_at      REAL NOT NULL,
  closed_at       REAL,
  draft_summary   TEXT,      -- unused since the Summariser service (kept for old rows)
  draft_fix       TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  seq             INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL,
  role            TEXT NOT NULL,       -- user | bot | agent | system
  author          TEXT NOT NULL,
  text            TEXT NOT NULL,
  decision_id     TEXT,                -- bot messages: link to the decision (why it answered/escalated)
  created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_by_conv ON messages(conversation_id, seq);
CREATE TABLE IF NOT EXISTS conversation_tickets (   -- one conversation can escalate more than once
  ticket_id       TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  linked_at       REAL NOT NULL
);
"""

_MESSAGES_SQL = """SELECT m.seq, m.role, m.author, m.text, m.decision_id, m.created_at,
                          d.decision, d.reason, d.confidence, d.threshold
                   FROM messages m LEFT JOIN decisions d ON d.decision_id = m.decision_id
                   WHERE m.conversation_id=? AND m.seq > ? ORDER BY m.seq"""


class ConversationStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        conn = connect(db_path)
        conn.executescript(DDL + OUTBOX_DDL)
        # backfill links for tickets raised before conversation_tickets existed (decision log has both ids)
        conn.execute("""INSERT OR IGNORE INTO conversation_tickets(ticket_id, conversation_id, linked_at)
                        SELECT ticket_id, conversation_id, created_at FROM decisions WHERE ticket_id IS NOT NULL""")
        conn.close()

    def _q(self, sql: str, args: tuple = (), one: bool = False):
        conn = connect(self.db_path)
        try:
            rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()
        return (rows[0] if rows else None) if one else rows

    def ensure(self, cid: str) -> dict:
        now = time.time()
        self._q("INSERT OR IGNORE INTO conversations(conversation_id, created_at, updated_at) VALUES (?,?,?)",
                (cid, now, now))
        return self.get(cid)

    def get(self, cid: str) -> dict | None:
        return self._q("SELECT * FROM conversations WHERE conversation_id=?", (cid,), one=True)

    def add(self, cid: str, role: str, author: str, text: str, decision_id: str | None = None) -> int:
        conn = connect(self.db_path)
        try:
            cur = conn.execute(
                "INSERT INTO messages(conversation_id, role, author, text, decision_id, created_at) VALUES (?,?,?,?,?,?)",
                (cid, role, author, text, decision_id, time.time()))
            conn.execute("UPDATE conversations SET updated_at=? WHERE conversation_id=?", (time.time(), cid))
            return cur.lastrowid
        finally:
            conn.close()

    def set(self, cid: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self._q(f"UPDATE conversations SET {cols}, updated_at=? WHERE conversation_id=?",
                (*fields.values(), time.time(), cid))

    def messages(self, cid: str, after: int = 0) -> list[dict]:
        """Messages with the bot's decision attached, so the transcript shows why it got stuck."""
        return self._q(_MESSAGES_SQL, (cid, after))

    def with_status(self, statuses: list[str]) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        return self._q(f"SELECT * FROM conversations WHERE status IN ({marks}) ORDER BY updated_at DESC",
                       tuple(statuses))

    def link_ticket(self, cid: str, ticket_id: str) -> None:
        """Remember every ticket a conversation raised; conversations.ticket_id is the latest."""
        self._q("INSERT OR IGNORE INTO conversation_tickets(ticket_id, conversation_id, linked_at) VALUES (?,?,?)",
                (ticket_id, cid, time.time()))
        self.set(cid, ticket_id=ticket_id)

    def by_ticket(self, ticket_id: str) -> dict | None:
        link = self._q("SELECT conversation_id FROM conversation_tickets WHERE ticket_id=?", (ticket_id,), one=True)
        if link:
            return self.get(link["conversation_id"])
        return self._q("SELECT * FROM conversations WHERE ticket_id=?", (ticket_id,), one=True)   # pre-link data

    def close(self, cid: str, agent_id: str, goodbye: str) -> bool:
        """Close a live chat and enqueue conversation.closed, atomically. False if already closed."""
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conv = conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (cid,)).fetchone()
            if conv is None or conv["status"] == "CLOSED":
                conn.execute("ROLLBACK")
                return False
            now = time.time()
            conn.execute("INSERT INTO messages(conversation_id, role, author, text, created_at) VALUES (?,?,?,?,?)",
                         (cid, "system", "helix", goodbye, now))
            conn.execute("UPDATE conversations SET status='CLOSED', closed_at=?, updated_at=? WHERE conversation_id=?",
                         (now, now, cid))
            transcript = [dict(r) for r in conn.execute(_MESSAGES_SQL, (cid, 0)).fetchall()]
            # Event-carried state: the transcript travels with the event, so the
            # Summariser never has to call back into Chat (and works if Chat is down).
            enqueue(conn, new_event("conversation.closed", cid, {
                "conversation_id": cid, "ticket_id": conv["ticket_id"], "agent_id": agent_id,
                "closed_at": now,
                "messages": [{k: m[k] for k in ("seq", "role", "author", "text", "decision", "reason",
                                                "confidence", "threshold")} for m in transcript],
            }))
            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
