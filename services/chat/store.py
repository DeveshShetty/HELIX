"""Decision log (SQLite, owned by Chat).

Every answer/escalate decision is recorded with its confidence score and the
threshold in force at the time, plus later human feedback. This is the
de-risking plan from our proposal: calibrate the threshold against real data
instead of guessing it.
"""
import json
import time

from common.db import connect

DDL = """
CREATE TABLE IF NOT EXISTS decisions (
  decision_id      TEXT PRIMARY KEY,
  conversation_id  TEXT NOT NULL,
  message          TEXT NOT NULL,
  decision         TEXT NOT NULL,
  reason           TEXT NOT NULL,
  confidence       REAL,
  threshold        REAL NOT NULL,
  citations        TEXT NOT NULL,
  ticket_id        TEXT,
  degraded         INTEGER NOT NULL,
  model            TEXT,
  index_version    TEXT,
  prompt_tokens    INTEGER NOT NULL,
  completion_tokens INTEGER NOT NULL,
  cost_usd         REAL NOT NULL,
  created_at       REAL NOT NULL,
  human_contradicted INTEGER,
  feedback_note    TEXT
);
"""


class DecisionStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        conn = connect(db_path)
        conn.executescript(DDL)
        conn.close()

    def save(self, d: dict) -> None:
        conn = connect(self.db_path)
        try:
            conn.execute(
                """INSERT INTO decisions(decision_id, conversation_id, message, decision, reason,
                   confidence, threshold, citations, ticket_id, degraded, model, index_version,
                   prompt_tokens, completion_tokens, cost_usd, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (d["decision_id"], d["conversation_id"], d["message"], d["decision"], d["reason"],
                 d["confidence"], d["threshold"], json.dumps(d["citations"]), d["ticket_id"],
                 int(d["degraded"]), d["model"], d["index_version"], d["tokens"]["prompt"],
                 d["tokens"]["completion"], d["cost_usd"], time.time()),
            )
        finally:
            conn.close()

    def set_ticket(self, decision_id: str, ticket_id: str) -> None:
        conn = connect(self.db_path)
        try:
            conn.execute("UPDATE decisions SET ticket_id=? WHERE decision_id=?", (ticket_id, decision_id))
        finally:
            conn.close()

    def recent(self, limit: int = 50) -> list[dict]:
        conn = connect(self.db_path)
        try:
            rows = conn.execute("SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            d = dict(r)
            d["citations"] = json.loads(d["citations"])
            d["degraded"] = bool(d["degraded"])
            out.append(d)
        return out

    def feedback(self, decision_id: str, contradicted: bool, note: str | None) -> bool:
        conn = connect(self.db_path)
        try:
            cur = conn.execute(
                "UPDATE decisions SET human_contradicted=?, feedback_note=? WHERE decision_id=?",
                (int(contradicted), note, decision_id))
            return cur.rowcount == 1
        finally:
            conn.close()
