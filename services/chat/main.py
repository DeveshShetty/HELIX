"""Chat / RAG Support Service.

For each message:
  1. injection check on the user's message            -> refused
  2. greetings / thanks / vague messages               -> reply, no ticket
  3. daily cost/token ceiling reached                  -> escalate (cost control)
  4. retrieve from the Knowledge Base                  (deadline + retry budget + circuit breaker)
       KB unavailable / circuit open                   -> escalate, degraded (never guess)
  5. look up the threshold calibrated for the index's EMBEDDING MODEL
       no calibrated threshold for that model          -> escalate
  6. sanitise retrieved passages (indirect injection)
  7. confidence from retrieval (deterministic per index version)
       confidence < threshold                          -> escalate
  8. per-query token budget exceeded                   -> escalate
  9. generate a grounded answer (simulated or Ollama)  -> answer
The answer-vs-escalate decision is taken in step 7 from retrieval scores,
BEFORE any text is generated, so it does not flip between runs.
Every escalation goes through an outbox (store.py) so it is never lost.
"""
import json
import os
import re
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from prometheus_client import Counter, Gauge, Histogram
from pydantic import BaseModel, Field

from common.circuit_breaker import CircuitOpenError
from common.db import start_daemon
from common.observability import instrument
from services.chat import clients, guard, llm
from services.chat.store import DecisionStore

SERVICE = "chat"
DB_PATH = os.getenv("DB_PATH", "/data/chat.db")
TOP_K = int(os.getenv("TOP_K", "3"))

# A threshold is only meaningful for the embedding model it was calibrated on:
# similarity scores from different models have different scales.
THRESHOLDS: dict[str, float] = json.loads(os.getenv("CONFIDENCE_THRESHOLDS", '{"hash-v1": 0.30}'))

MAX_PROMPT_TOKENS = int(os.getenv("MAX_PROMPT_TOKENS", "1500"))          # per query
DAILY_TOKEN_CEILING = int(os.getenv("DAILY_TOKEN_CEILING", "200000"))     # per UTC day
DAILY_COST_CEILING_USD = float(os.getenv("DAILY_COST_CEILING_USD", "0.50"))

app = FastAPI(title="Chat / RAG Support Service")
instrument(app, SERVICE)
store = DecisionStore(DB_PATH)

DECISIONS = Counter("chat_decisions_total", "Answer/escalate/refuse decisions", ["decision", "reason"])
CONFIDENCE = Histogram("chat_confidence", "Confidence score per decision",
                       buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0))
TOKENS = Counter("llm_tokens_total", "LLM tokens used", ["model", "kind"])
COST = Counter("llm_cost_usd_total", "Notional LLM cost in USD", ["model"])
DAILY_SPEND = Gauge("llm_daily_spend_usd", "Notional LLM spend so far today (UTC)")
DAILY_CEILING = Gauge("llm_daily_ceiling_usd", "Daily LLM cost ceiling")
ESCALATIONS_PENDING = Gauge("escalations_pending", "Escalations waiting for the Ticket Service")
DAILY_CEILING.set(DAILY_COST_CEILING_USD)

# ------------------------------------------------------------------ text helpers

STOPWORDS = set("""a an the is are was were be been am i me my we our you your it its
to of in on at for from by with and or not no do does did how what why when where which
who can could should would will shall may might this that these those there here
keep keeps get gets got any some all just please help hi hello really back""".split())
_TOKEN = re.compile(r"[a-z0-9]+")


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def content_terms(text: str) -> set[str]:
    return {_stem(w) for w in _TOKEN.findall(text.lower()) if w not in STOPWORDS}


CAPABILITIES = ("I'm the Helix Cloud support assistant. Ask me about payments, logins and 2FA, "
                "storage, billing or the API and I'll answer from our runbooks, FAQs and past "
                "incidents. If I'm not sure, I'll open a ticket for an on-call engineer instead of guessing.")
SMALL_TALK = [  # (pattern, reply) - only used when the message has NO other content
    (re.compile(r"\b(hi|hello|hey|hiya|howdy|yo|good\s+(morning|afternoon|evening))\b"), "Hi! " + CAPABILITIES),
    (re.compile(r"\b(thanks|thank\s+you|thx|ty|cheers|great|perfect|awesome|ok|okay)\b"),
     "You're welcome! Is there anything else I can help with?"),
    (re.compile(r"\b(bye|goodbye|see\s+you|cya)\b"), "Goodbye! Come back any time you need help."),
    (re.compile(r"\b(who|what)\s+are\s+you\b|\bwhat\s+can\s+you\s+do\b|^\s*help\s*[?!.]*\s*$"), CAPABILITIES),
]
SMALL_TALK_WORDS = set("""hi hello hey hiya howdy yo good morning afternoon evening thanks thank thx ty
cheers great perfect awesome ok okay bye goodbye see cya who what are you can do help there
worked fixed solved sorted nice cool much lot""".split())   # NOT "working": "not working" is a real problem
CLARIFY = ("Could you tell me a bit more? For example, what were you trying to do, and what error "
           "or behaviour did you see?")


def small_talk_reply(text: str) -> str | None:
    """Reply for greetings/thanks/vague messages, or None if this is a real question.

    A message is small talk only if nothing is left once greeting words and
    stopwords are removed, so "hi, my payment api returns 502" is NOT small talk.
    Without this step every "hi" found no document and opened a ticket.
    """
    lowered = text.lower()
    words = _TOKEN.findall(lowered)
    if any(w not in STOPWORDS and w not in SMALL_TALK_WORDS for w in words):
        return None
    for pattern, reply in SMALL_TALK:
        if pattern.search(lowered):
            return reply
    return CLARIFY


IDENTIFIER_PENALTY = float(os.getenv("IDENTIFIER_PENALTY", "0.5"))


def compute_confidence(question: str, passage: dict) -> tuple[float, float]:
    """confidence = (0.5 * retrieval score + 0.5 * coverage) * identifier penalty.

    Built only from retrieval, never from model output, so it is fixed for a
    given (question, index version): the decision cannot flip between runs.
    coverage   = share of the question's content words found in the top passage.
    identifier = any token with a digit (error codes, versions: 401, 502, v2).
                 If the question names one the passage doesn't contain, the
                 passage is about something else ("payment api returns 401"
                 vs the 502 runbook), so confidence is multiplied by
                 IDENTIFIER_PENALTY.
    """
    q = content_terms(question)
    p = content_terms(passage["title"] + " " + passage["text"])
    coverage = len(q & p) / len(q) if q else 0.0
    score = min(1.0, max(0.0, float(passage["score"])))
    confidence = 0.5 * score + 0.5 * coverage
    if any(any(ch.isdigit() for ch in t) and t not in p for t in q):
        confidence *= IDENTIFIER_PENALTY
    return round(confidence, 3), round(coverage, 3)


def utc_midnight() -> float:
    now = time.time()
    return now - (now % 86400)


# ------------------------------------------------------------------ escalation (outbox)

def try_open_ticket(decision_id: str, payload: dict) -> str | None:
    """One attempt to turn a pending escalation into a ticket. Returns the ticket id or None."""
    try:
        t = clients.open_ticket(f"chat-{decision_id}", payload)
    except Exception as exc:
        store.escalation_failed(decision_id, repr(exc))
        return None
    store.escalation_done(decision_id, t["ticket_id"])
    return t["ticket_id"]


def escalation_worker() -> None:
    """Retry escalations the Ticket Service could not take. Same Idempotency-Key every time."""
    while True:
        try:
            for decision_id, payload in store.due_escalations():
                tid = try_open_ticket(decision_id, payload)
                if tid:
                    print(f"[escalation] {decision_id} delivered late as {tid}", flush=True)
            ESCALATIONS_PENDING.set(store.pending_count())
        except Exception as exc:
            print(f"[escalation] worker error: {exc!r}", flush=True)
        time.sleep(2)


start_daemon(escalation_worker, name="chat-escalation-worker")


def _escalate(d: dict, question: str, retrieved_ids: list[str], why: str) -> dict:
    d["decision"] = "escalate"
    if d.get("dry_run"):
        d["answer"] = f"{why} (dry run: no ticket opened)"
        return d
    payload = {"title": question[:120], "question": question, "severity": "medium",
               "conversation_id": d["conversation_id"], "confidence": d["confidence"],
               "retrieved_doc_ids": retrieved_ids}
    # Durable FIRST: if we crash or Tickets is down after this line, the worker delivers it.
    store.add_escalation(d["decision_id"], payload)
    tid = try_open_ticket(d["decision_id"], payload)
    if tid:
        d["ticket_id"] = tid
        d["answer"] = f"{why} I've opened ticket {tid} and an on-call engineer will follow up."
    else:
        d["degraded"] = True
        d["escalation_pending"] = True
        d["answer"] = (f"{why} Our ticket system is busy, but your request is saved (reference "
                       f"{d['decision_id']}) and a ticket will be opened automatically.")
    return d


# ------------------------------------------------------------------ API

class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None
    # Evaluation mode: identical decision logic, but an escalation does NOT open a
    # ticket (running the 50-question test set must not page on-call 20 times).
    dry_run: bool = False


class FeedbackIn(BaseModel):
    human_contradicted: bool
    note: str | None = None


@app.post("/chat")
def chat(body: ChatIn):
    question = body.message.strip()
    d = {
        "decision_id": f"d-{uuid.uuid4().hex[:12]}",
        "conversation_id": body.conversation_id or f"c-{uuid.uuid4().hex[:8]}",
        "message": question, "decision": "escalate", "answer": "", "confidence": None,
        "threshold": None, "citations": [], "ticket_id": None, "escalation_pending": False,
        "degraded": False, "reason": "low_confidence", "model": None,
        "prompt_version": llm.PROMPT_VERSION, "index_version": None, "embedding_model": None,
        "sources": [], "tokens": {"prompt": 0, "completion": 0}, "cost_usd": 0.0,
        "dry_run": body.dry_run,
    }

    # 1. direct prompt injection in the user's message
    if guard.looks_like_injection(question):
        d.update(decision="refused", reason="injection_detected",
                 answer="I can't help with that request. I can only answer Helix Cloud support questions.")
        return _finish(d)

    # 2. greetings, thanks, vague messages: reply directly, no search, no ticket
    reply = small_talk_reply(question)
    if reply is not None:
        d.update(decision="answer", answer=reply,
                 reason="needs_clarification" if reply == CLARIFY else "small_talk")
        return _finish(d)

    # 3. daily ceiling: stop spending on the LLM, hand over to humans
    tokens_today, cost_today = store.spend_since(utc_midnight())
    DAILY_SPEND.set(cost_today)
    if cost_today >= DAILY_COST_CEILING_USD or tokens_today >= DAILY_TOKEN_CEILING:
        d["reason"] = "daily_budget_exhausted"
        return _finish(_escalate(d, question, [], "I've reached today's automated-answer budget, so I won't guess."))

    # 4. retrieve, behind deadline + retry budget + circuit breaker
    try:
        kb = clients.search_kb(question, TOP_K)
    except CircuitOpenError:
        d.update(reason="kb_unavailable", degraded=True)
        return _finish(_escalate(d, question, [], "I can't search the knowledge base right now (circuit open), so I won't guess."))
    except Exception as exc:
        print(f"[chat] knowledge search failed: {exc!r}", flush=True)
        d.update(reason="kb_unavailable", degraded=True)
        return _finish(_escalate(d, question, [], "I can't search the knowledge base right now, so I won't guess."))

    d["index_version"] = kb.get("index_version")
    d["embedding_model"] = kb.get("embedding_model")
    results = kb.get("results", [])

    # 5. the threshold belongs to the embedding model it was calibrated on
    d["threshold"] = THRESHOLDS.get(d["embedding_model"])
    if d["threshold"] is None:
        d["reason"] = "uncalibrated_model"
        return _finish(_escalate(d, question, [r["doc_id"] for r in results],
                                 "My search model has changed and hasn't been calibrated yet, so I won't guess."))

    # 6. indirect injection: sanitise every retrieved passage
    passages = []
    for r in results:
        clean, removed = guard.sanitize_document(r["text"])
        if removed:
            print(f"[guard] removed {removed} injected sentence(s) from {r['doc_id']}", flush=True)
        if clean:
            passages.append({**r, "text": clean})
    retrieved_ids = [p["doc_id"] for p in passages]
    if not passages:
        d["confidence"] = 0.0
        return _finish(_escalate(d, question, [], "I couldn't find anything about that in our knowledge base."))

    # 7. confidence vs threshold (retrieval only: deterministic per index version)
    d["confidence"], _coverage = compute_confidence(question, passages[0])
    top = passages[0]
    d["sources"] = [{"doc_id": top["doc_id"], "origin": top.get("origin"), "author": top.get("author"),
                     "embedding_model": top.get("embedding_model")}]
    if d["confidence"] < d["threshold"]:
        return _finish(_escalate(d, question, retrieved_ids, "I'm not confident I have the right answer for that."))

    # 8. per-query token budget
    if llm.prompt_tokens(question, top) > MAX_PROMPT_TOKENS:
        d["reason"] = "token_budget_exceeded"
        return _finish(_escalate(d, question, retrieved_ids, "That needs more context than I'm allowed to use per question."))

    # 9. grounded answer from the top passage
    gen = llm.generate(question, passages[:1])
    _account(d, gen)
    if guard.looks_like_injection(gen["text"]):
        d["reason"] = "unsafe_output"
        return _finish(_escalate(d, question, retrieved_ids, "I couldn't produce a safe, grounded answer."))
    d.update(decision="answer", reason="grounded", answer=gen["text"],
             citations=[top["doc_id"]], degraded=gen["fallback"])
    return _finish(d)


def _account(d: dict, gen: dict) -> None:
    d["model"] = gen["model"]
    d["tokens"] = {"prompt": gen["prompt_tokens"], "completion": gen["completion_tokens"]}
    d["cost_usd"] = llm.cost_usd(gen["prompt_tokens"], gen["completion_tokens"])
    TOKENS.labels(gen["model"], "prompt").inc(gen["prompt_tokens"])
    TOKENS.labels(gen["model"], "completion").inc(gen["completion_tokens"])
    COST.labels(gen["model"]).inc(d["cost_usd"])


def _finish(d: dict) -> dict:
    DECISIONS.labels(d["decision"], d["reason"]).inc()
    if d["confidence"] is not None:
        CONFIDENCE.observe(d["confidence"])
    store.save(d)
    print(f"[decision] {d['decision_id']} {d['decision']}/{d['reason']} conf={d['confidence']} "
          f"thr={d['threshold']} emb={d['embedding_model']} ticket={d['ticket_id']}", flush=True)
    return {k: v for k, v in d.items() if k != "message"}


@app.get("/decisions")
def decisions(limit: int = 50):
    return store.recent(min(limit, 500))


@app.get("/decisions/{decision_id}")
def decision(decision_id: str):
    d = store.get(decision_id)
    if not d:
        raise HTTPException(404, "decision not found")
    return d


@app.post("/decisions/{decision_id}/feedback")
def feedback(decision_id: str, body: FeedbackIn):
    if not store.feedback(decision_id, body.human_contradicted, body.note):
        raise HTTPException(404, "decision not found")
    return {"decision_id": decision_id, "recorded": True}


@app.get("/config")
def config():
    tokens_today, cost_today = store.spend_since(utc_midnight())
    return {"thresholds": THRESHOLDS, "llm_mode": llm.LLM_MODE,
            "model": llm.OLLAMA_MODEL if llm.LLM_MODE == "ollama" else "simulated",
            "prompt_version": llm.PROMPT_VERSION, "kb_breaker": clients.kb_breaker.state.value,
            "budget": {"max_prompt_tokens": MAX_PROMPT_TOKENS, "daily_token_ceiling": DAILY_TOKEN_CEILING,
                       "daily_cost_ceiling_usd": DAILY_COST_CEILING_USD, "tokens_today": tokens_today,
                       "cost_today_usd": round(cost_today, 6)},
            "escalations_pending": store.pending_count()}


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE}


@app.get("/", include_in_schema=False)
def ui():
    return FileResponse(Path(__file__).parent / "static" / "index.html")
