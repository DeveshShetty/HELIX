"""Chat's own database: decision log + escalation outbox.

decisions: every answer/escalate/refuse decision with the confidence, the
  threshold in force, and the model / prompt / index versions that produced
  it, so any answer can be traced back and the threshold re-calibrated.

pending_escalations: an OUTBOX for escalations. Every failure path ends in an
  escalation, so it must be the most reliable path: the escalation is written
  here BEFORE calling the Ticket Service. If Tickets is down, a background
  worker keeps retrying with the same Idempotency-Key until it succeeds, so
  the escalation is never lost and never duplicated.
"""
import json
import time

from common import retry
from common.db import connect

DDL = """
CREATE TABLE IF NOT EXISTS decisions (
  decision_id       TEXT PRIMARY KEY,
  conversation_id   TEXT NOT NULL,
  message           TEXT NOT NULL,
  decision          TEXT NOT NULL,
  reason            TEXT NOT NULL,
  confidence        REAL,
  threshold         REAL,
  citations         TEXT NOT NULL,
  ticket_id         TEXT,
  degraded          INTEGER NOT NULL,
  model             TEXT,
  prompt_version    TEXT,
  index_version     TEXT,
  embedding_model   TEXT,
  prompt_tokens     INTEGER NOT NULL,
  completion_tokens INTEGER NOT NULL,
  cost_usd          REAL NOT NULL,
  created_at        REAL NOT NULL,
  human_contradicted INTEGER,
  feedback_note     TEXT
);
CREATE TABLE IF NOT EXISTS pending_escalations (
  decision_id     TEXT PRIMARY KEY,
  payload         TEXT NOT NULL,
  status          TEXT NOT NULL,      -- PENDING | DONE
  attempts        INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL,
  last_error      TEXT,
  ticket_id       TEXT,
  created_at      REAL NOT NULL
);
"""


class DecisionStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        conn = connect(db_path)
        conn.executescript(DDL)
        conn.close()

    # ------------------------------------------------------------ decisions
    def save(self, d: dict) -> None:
        conn = connect(self.db_path)
        try:
            conn.execute(
                """INSERT INTO decisions(decision_id, conversation_id, message, decision, reason,
                   confidence, threshold, citations, ticket_id, degraded, model, prompt_version,
                   index_version, embedding_model, prompt_tokens, completion_tokens, cost_usd, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (d["decision_id"], d["conversation_id"], d["message"], d["decision"], d["reason"],
                 d["confidence"], d["threshold"], json.dumps(d["citations"]), d["ticket_id"],
                 int(d["degraded"]), d["model"], d["prompt_version"], d["index_version"],
                 d["embedding_model"], d["tokens"]["prompt"], d["tokens"]["completion"],
                 d["cost_usd"], time.time()),
            )
        finally:
            conn.close()

    def get(self, decision_id: str) -> dict | None:
        conn = connect(self.db_path)
        try:
            row = conn.execute("SELECT * FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
            pending = conn.execute("SELECT status, attempts, last_error FROM pending_escalations WHERE decision_id=?",
                                   (decision_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        d = self._row(row)
        d["escalation"] = dict(pending) if pending else None
        return d

    def recent(self, limit: int = 50) -> list[dict]:
        conn = connect(self.db_path)
        try:
            rows = conn.execute("SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        finally:
            conn.close()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["citations"] = json.loads(d["citations"])
        d["degraded"] = bool(d["degraded"])
        return d

    def feedback(self, decision_id: str, contradicted: bool, note: str | None) -> bool:
        conn = connect(self.db_path)
        try:
            cur = conn.execute(
                "UPDATE decisions SET human_contradicted=?, feedback_note=? WHERE decision_id=?",
                (int(contradicted), note, decision_id))
            return cur.rowcount == 1
        finally:
            conn.close()

    def spend_since(self, since: float) -> tuple[int, float]:
        """(tokens, cost_usd) spent on LLM calls since a timestamp: drives the daily ceiling."""
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0), COALESCE(SUM(cost_usd), 0) "
                "FROM decisions WHERE created_at >= ?", (since,)).fetchone()
        finally:
            conn.close()
        return int(row[0]), float(row[1])

    # ------------------------------------------------------------ escalation outbox
    def add_escalation(self, decision_id: str, payload: dict, first_retry_in: float = 10.0) -> None:
        # first_retry_in: the caller makes the first attempt itself; the worker only
        # steps in if that fails (a race would be harmless anyway: same Idempotency-Key).
        conn = connect(self.db_path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO pending_escalations(decision_id, payload, status, next_attempt_at, created_at) "
                "VALUES (?,?,'PENDING',?,?)",
                (decision_id, json.dumps(payload), time.time() + first_retry_in, time.time()))
        finally:
            conn.close()

    def escalation_done(self, decision_id: str, ticket_id: str) -> None:
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE pending_escalations SET status='DONE', ticket_id=? WHERE decision_id=?",
                         (ticket_id, decision_id))
            conn.execute("UPDATE decisions SET ticket_id=? WHERE decision_id=?", (ticket_id, decision_id))
            conn.execute("COMMIT")
        finally:
            conn.close()

    def escalation_failed(self, decision_id: str, error: str, max_attempts: int = 10,
                          backoff_cap: float = 60.0) -> str:
        """Record a failed delivery. Returns "retry" or "gave_up".

        Retries use the shared jittered backoff; after max_attempts the escalation is
        marked FAILED (no more retries) so the caller can alert, instead of retrying forever.
        """
        conn = connect(self.db_path)
        try:
            row = conn.execute("SELECT attempts FROM pending_escalations WHERE decision_id=?",
                               (decision_id,)).fetchone()
            attempts = (row["attempts"] if row else 0) + 1
            if attempts >= max_attempts:
                conn.execute("UPDATE pending_escalations SET status='FAILED', attempts=?, last_error=? "
                             "WHERE decision_id=?", (attempts, error[:300], decision_id))
                return "gave_up"
            delay = retry.backoff(attempts, 2.0, backoff_cap)
            conn.execute("UPDATE pending_escalations SET attempts=?, last_error=?, next_attempt_at=? "
                         "WHERE decision_id=?", (attempts, error[:300], time.time() + delay, decision_id))
            return "retry"
        finally:
            conn.close()

    def failed_count(self) -> int:
        conn = connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM pending_escalations WHERE status='FAILED'").fetchone()[0]
        finally:
            conn.close()

    def due_escalations(self, limit: int = 20) -> list[tuple[str, dict]]:
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT decision_id, payload FROM pending_escalations WHERE status='PENDING' "
                "AND next_attempt_at <= ? ORDER BY created_at LIMIT ?", (time.time(), limit)).fetchall()
        finally:
            conn.close()
        return [(r["decision_id"], json.loads(r["payload"])) for r in rows]

    def pending_count(self) -> int:
        conn = connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM pending_escalations WHERE status='PENDING'").fetchone()[0]
        finally:
            conn.close()
