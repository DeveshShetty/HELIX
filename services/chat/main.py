"""Chat / RAG Support Service (owner: Devesh Shetty)

Conversational front door. For each message:
  1. injection check on the user's message           -> refused
  2. retrieve passages from the Knowledge Base        (timeout + retry + circuit breaker)
       KB unavailable / circuit open                  -> escalate, degraded (never guess)
  3. sanitise retrieved passages (indirect injection)
  4. confidence = f(retrieval score, query coverage)
       confidence < CONFIDENCE_THRESHOLD              -> escalate (open a ticket)
  5. generate a grounded answer (simulated or Ollama) -> answer
Every decision is logged with its confidence and the threshold in force.
"""
import os
import re
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from prometheus_client import Counter, Histogram
from pydantic import BaseModel, Field

from common.circuit_breaker import CircuitOpenError
from common.observability import instrument
from services.chat import clients, guard, llm
from services.chat.store import DecisionStore

SERVICE = "chat"
DB_PATH = os.getenv("DB_PATH", "/data/chat.db")
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.45"))
TOP_K = int(os.getenv("TOP_K", "3"))

app = FastAPI(title="Chat / RAG Support Service")
instrument(app, SERVICE)
store = DecisionStore(DB_PATH)

DECISIONS = Counter("chat_decisions_total", "Answer/escalate/refuse decisions", ["decision", "reason"])
CONFIDENCE = Histogram("chat_confidence", "Confidence score per decision",
                       buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0))
TOKENS = Counter("llm_tokens_total", "LLM tokens used", ["model", "kind"])
COST = Counter("llm_cost_usd_total", "Notional LLM cost in USD", ["model"])

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

    coverage   = share of the question's content words found in the top passage.
    identifier = any token with a digit (error codes, versions: 401, 502, v2).
                 If the question names one the passage doesn't contain, the
                 passage is about something else ("payment api returns 401"
                 vs the 502 runbook), so confidence is multiplied by
                 IDENTIFIER_PENALTY.
    Weights, penalty and threshold are tunable; calibrate with eval/run_eval.py.
    """
    q = content_terms(question)
    p = content_terms(passage["title"] + " " + passage["text"])
    coverage = len(q & p) / len(q) if q else 0.0
    score = min(1.0, max(0.0, float(passage["score"])))
    confidence = 0.5 * score + 0.5 * coverage
    if any(any(ch.isdigit() for ch in t) and t not in p for t in q):
        confidence *= IDENTIFIER_PENALTY
    return round(confidence, 3), round(coverage, 3)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None


class FeedbackIn(BaseModel):
    human_contradicted: bool
    note: str | None = None


def _escalate(d: dict, question: str, retrieved_ids: list[str], why: str) -> dict:
    """Open a ticket (idempotent on decision_id) and phrase the reply."""
    try:
        t = clients.open_ticket(f"chat-{d['decision_id']}", {
            "title": question[:120], "question": question, "severity": "medium",
            "conversation_id": d["conversation_id"], "confidence": d["confidence"],
            "retrieved_doc_ids": retrieved_ids,
        })
        d["ticket_id"] = t["ticket_id"]
        d["answer"] = f"{why} I've opened ticket {t['ticket_id']} and an on-call engineer will follow up."
    except Exception as exc:
        print(f"[chat] could not open ticket: {exc!r}", flush=True)
        d["degraded"] = True
        d["answer"] = (f"{why} I also couldn't open a ticket right now. "
                       "Please try again in a few minutes. I won't guess an answer.")
    return d


@app.post("/chat")
def chat(body: ChatIn):
    question = body.message.strip()
    d = {
        "decision_id": f"d-{uuid.uuid4().hex[:12]}",
        "conversation_id": body.conversation_id or f"c-{uuid.uuid4().hex[:8]}",
        "message": question, "decision": "escalate", "answer": "", "confidence": None,
        "threshold": CONFIDENCE_THRESHOLD, "citations": [], "ticket_id": None,
        "degraded": False, "reason": "low_confidence", "model": None, "index_version": None,
        "tokens": {"prompt": 0, "completion": 0}, "cost_usd": 0.0,
    }

    # 1. direct prompt injection in the user's message
    if guard.looks_like_injection(question):
        d.update(decision="refused", reason="injection_detected",
                 answer="I can't help with that request. I can only answer Helix Cloud support questions.")
        return _finish(d)

    # 1b. greetings, thanks, vague messages: reply directly, no search, no ticket
    reply = small_talk_reply(question)
    if reply is not None:
        d.update(decision="answer", answer=reply,
                 reason="needs_clarification" if reply == CLARIFY else "small_talk")
        return _finish(d)

    # 2. retrieve, behind timeout + retry + circuit breaker
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
    results = kb.get("results", [])

    # 3. indirect injection: sanitise every retrieved passage
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

    # 4. confidence vs threshold
    d["confidence"], _coverage = compute_confidence(question, passages[0])
    if d["confidence"] < CONFIDENCE_THRESHOLD:
        return _finish(_escalate(d, question, retrieved_ids, "I'm not confident I have the right answer for that."))

    # 5. grounded answer from the top passage(s)
    gen = llm.generate(question, passages[:1])
    if guard.looks_like_injection(gen["text"]) or gen["text"].strip().lower().startswith("i don't know"):
        d.update(reason="ungrounded_output")
        _account(d, gen)
        return _finish(_escalate(d, question, retrieved_ids, "I couldn't produce a safe, grounded answer."))
    _account(d, gen)
    d.update(decision="answer", reason="grounded", answer=gen["text"],
             citations=[passages[0]["doc_id"]], degraded=gen["fallback"])
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
          f"thr={d['threshold']} ticket={d['ticket_id']}", flush=True)
    return {k: v for k, v in d.items() if k != "message"}


@app.get("/decisions")
def decisions(limit: int = 50):
    return store.recent(min(limit, 500))


@app.post("/decisions/{decision_id}/feedback")
def feedback(decision_id: str, body: FeedbackIn):
    if not store.feedback(decision_id, body.human_contradicted, body.note):
        raise HTTPException(404, "decision not found")
    return {"decision_id": decision_id, "recorded": True}


@app.get("/config")
def config():
    return {"confidence_threshold": CONFIDENCE_THRESHOLD, "llm_mode": llm.LLM_MODE,
            "model": llm.OLLAMA_MODEL if llm.LLM_MODE == "ollama" else "simulated",
            "kb_breaker": clients.kb_breaker.state.value}


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE}


@app.get("/", include_in_schema=False)
def ui():
    return FileResponse(Path(__file__).parent / "static" / "index.html")
