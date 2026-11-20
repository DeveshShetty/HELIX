"""Chat / RAG Support Service.

For each message:
  1. injection check on the user's message            -> refused
  2. greetings / thanks / vague messages               -> reply, no ticket
  3. daily cost/token ceiling reached                  -> escalate (cost control)
  4. retrieve from the Knowledge Base                  (deadline + retry budget + circuit breaker)
       KB unavailable / circuit open                   -> escalate, degraded (never guess)
  5. look up the threshold calibrated for this INDEX VERSION (else its embedding model)
       no calibrated threshold                         -> escalate
  6. sanitise retrieved passages (indirect injection)
  7. confidence from retrieval (deterministic per index version)
       confidence < threshold                          -> escalate
  8. per-query token budget exceeded                   -> escalate
  9. generate a grounded answer (simulated or Ollama)  -> answer
The answer-vs-escalate decision is taken in step 7 from retrieval scores,
BEFORE any text is generated, so it does not flip between runs.
Every escalation goes through an outbox (store.py) so it is never lost.
"""
import logging
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
from common.db import relay_forever, start_daemon
from common.health import report as health_report
from common.observability import instrument
from common import budget, guard, log
from services.chat import clients, llm
from services.chat.conversations import ConversationStore
from services.chat.store import DecisionStore

logger = logging.getLogger("chat")

SERVICE = "chat"
DB_PATH = os.getenv("DB_PATH", "/data/chat.db")
TOP_K = int(os.getenv("TOP_K", "3"))

# A threshold is only meaningful for the index it was calibrated on: scores from different
# models have different scales, and even a rebuild with the same model changes them (new
# vocabulary/IDF, re-chunking). Keys are index versions ("hash-v1-20261117T190438") or, as a
# fallback, embedding models ("hash-v1"). Lookup: exact index version first; then the model,
# unless THRESHOLD_STRICT=1, in which case an index nobody calibrated always escalates.
# Every decision is stamped with the key that was used (threshold_key).
THRESHOLDS: dict[str, float] = json.loads(os.getenv("CONFIDENCE_THRESHOLDS", '{"hash-v1": 0.30}'))
THRESHOLD_STRICT = os.getenv("THRESHOLD_STRICT", "0") == "1"


def threshold_for(index_version: str | None, embedding_model: str | None) -> tuple[float | None, str | None]:
    """(threshold, key used). key is "index:<version>", "model:<name>" or None (uncalibrated)."""
    if index_version in THRESHOLDS:
        return THRESHOLDS[index_version], f"index:{index_version}"
    if not THRESHOLD_STRICT and embedding_model in THRESHOLDS:
        return THRESHOLDS[embedding_model], f"model:{embedding_model}"
    return None, None

MAX_PROMPT_TOKENS = int(os.getenv("MAX_PROMPT_TOKENS", "1500"))          # per query
# Daily ceilings are SHARED with Knowledge and the Summariser (common/budget.py).
DAILY_TOKEN_CEILING = budget.DAILY_TOKEN_CEILING                          # per UTC day, all services
DAILY_COST_CEILING_USD = budget.DAILY_COST_CEILING_USD

# Pages are small and change with every release: never let a browser serve a stale copy.
NO_CACHE = {"Cache-Control": "no-store"}

app = FastAPI(title="Chat / RAG Support Service")
instrument(app, SERVICE)
store = DecisionStore(DB_PATH)
convs = ConversationStore(DB_PATH)      # after DecisionStore: transcripts join the decisions table

DECISIONS = Counter("chat_decisions_total", "Answer/escalate/refuse decisions", ["decision", "reason"])
CONFIDENCE = Histogram("chat_confidence", "Confidence score per decision",
                       buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0))
TOKENS = Counter("llm_tokens_total", "LLM tokens used", ["model", "kind"])
COST = Counter("llm_cost_usd_total", "Notional LLM cost in USD", ["model"])
DAILY_SPEND = Gauge("llm_daily_spend_usd", "Notional LLM spend so far today (UTC)")
DAILY_CEILING = Gauge("llm_daily_ceiling_usd", "Daily LLM cost ceiling")
THRESHOLD_LOOKUPS = Counter("chat_threshold_lookups_total",
                            "Which threshold key answered: index (calibrated for this index version), "
                            "model (fallback) or none (uncalibrated -> escalate)", ["source"])
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
    # + quantifiers tolerate stretched spellings: "hii", "heyyy", "hellooo", "thanksss", "okk"
    (re.compile(r"\b(h+i+|he+l+o+|he+y+|hiya|howdy|yo+|good\s+(morning|afternoon|evening))\b"), "Hi! " + CAPABILITIES),
    (re.compile(r"\b(than+k+s+|thank\s+you|thx|ty|cheers|great|perfect|awesome|o+k+|okay+)\b"),
     "You're welcome! Is there anything else I can help with?"),
    (re.compile(r"\b(bye+|goodbye|see\s+you|cya)\b"), "Goodbye! Come back any time you need help."),
    (re.compile(r"\b(who|what)\s+are\s+you\b|\bwhat\s+can\s+you\s+do\b|^\s*help\s*[?!.]*\s*$"), CAPABILITIES),
]
SMALL_TALK_WORDS = set("""hi hello hey hiya howdy yo good morning afternoon evening thanks thank thx ty
cheers great perfect awesome ok okay bye goodbye see cya who what are you can do help there
worked fixed solved sorted nice cool much lot""".split())   # NOT "working": "not working" is a real problem
def _squash(word: str) -> str:
    """Collapse repeated letters so stretched spellings match: hii -> hi, heyyy -> hey, hellooo -> helo."""
    return re.sub(r"(.)\1+", r"\1", word)


_SMALL_TALK_SQUASHED = {_squash(w) for w in SMALL_TALK_WORDS}
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
    if any(w not in STOPWORDS and _squash(w) not in _SMALL_TALK_SQUASHED for w in words):
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


# ------------------------------------------------------------------ escalation (outbox)

ESCALATION_MAX_ATTEMPTS = int(os.getenv("ESCALATION_MAX_ATTEMPTS", "10"))
ESCALATIONS_FAILED = Counter("escalations_failed_total", "Escalations given up after ESCALATION_MAX_ATTEMPTS")


def try_open_ticket(decision_id: str, payload: dict) -> str | None:
    """One attempt to turn a pending escalation into a ticket. Returns the ticket id or None."""
    try:
        t = clients.open_ticket(f"chat-{decision_id}", payload)
    except Exception as exc:
        if store.escalation_failed(decision_id, repr(exc), max_attempts=ESCALATION_MAX_ATTEMPTS) == "gave_up":
            ESCALATIONS_FAILED.inc()
            logger.error(f"[escalation] ERROR gave up on {decision_id} after {ESCALATION_MAX_ATTEMPTS} attempts: "
                  f"{exc!r}; customer request needs manual follow-up")
        return None
    store.escalation_done(decision_id, t["ticket_id"])
    if payload.get("conversation_id"):
        convs.link_ticket(payload["conversation_id"], t["ticket_id"])
    return t["ticket_id"]


def escalation_worker() -> None:
    """Retry escalations the Ticket Service could not take. Same Idempotency-Key every time."""
    while True:
        try:
            for decision_id, payload in store.due_escalations():
                log.set_id(payload.get("correlation_id") or decision_id)
                tid = try_open_ticket(decision_id, payload)
                if tid:
                    logger.info(f"[escalation] {decision_id} delivered late as {tid}")
            ESCALATIONS_PENDING.set(store.pending_count())
        except Exception as exc:
            logger.error(f"[escalation] worker error: {exc!r}")
        log.set_id(None)
        time.sleep(2)


start_daemon(escalation_worker, name="chat-escalation-worker")
start_daemon(relay_forever, DB_PATH, name="chat-outbox-relay")     # publishes conversation.closed


def _escalate(d: dict, question: str, retrieved_ids: list[str], why: str) -> dict:
    d["decision"] = "escalate"
    if d.get("dry_run"):
        d["answer"] = f"{why} (dry run: no ticket opened)"
        return d
    payload = {"title": question[:120], "question": question, "severity": "medium",
               "conversation_id": d["conversation_id"], "confidence": d["confidence"],
               "retrieved_doc_ids": retrieved_ids,
               "correlation_id": log.current()}      # the late retry keeps the customer's request id
    # Durable FIRST: if we crash or Tickets is down after this line, the worker delivers it.
    store.add_escalation(d["decision_id"], payload)
    tid = try_open_ticket(d["decision_id"], payload)
    if tid:
        d["ticket_id"] = tid
        d["answer"] = (f"{why} I've passed your question to our support team. "
                       f"Your reference is {tid}, and an engineer will get back to you.")
    else:
        d["degraded"] = True
        d["escalation_pending"] = True
        d["answer"] = (f"{why} Our support system is busy right now, but your question is saved "
                       f"(reference {d['decision_id']}) and will reach the team automatically.")
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
        # 128 random bits: the conversation ID is what lets a browser reopen the chat later
        "conversation_id": body.conversation_id or f"c-{uuid.uuid4().hex}",
        "message": question, "decision": "escalate", "answer": "", "confidence": None,
        "threshold": None, "citations": [], "ticket_id": None, "escalation_pending": False,
        "degraded": False, "reason": "low_confidence", "model": None,
        "prompt_version": llm.PROMPT_VERSION, "index_version": None, "embedding_model": None, "threshold_key": None,
        "sources": [], "tokens": {"prompt": 0, "completion": 0}, "cost_usd": 0.0, "request_id": log.current(),
        "dry_run": body.dry_run,
    }

    # 0. conversation log; while a human agent is handling the chat, the bot stays quiet
    if not body.dry_run:
        conv = convs.ensure(d["conversation_id"])
        convs.add(d["conversation_id"], "user", "customer", question)
        if conv["status"] in ("WAITING_AGENT", "LIVE"):
            return {"conversation_id": d["conversation_id"], "decision": "live", "reason": "agent_handling",
                    "answer": "", "status": conv["status"], "ticket_id": conv["ticket_id"]}

    # 1. direct prompt injection in the user's message
    if guard.looks_like_injection(question):
        d.update(decision="refused", reason="injection_detected",
                 answer="Sorry, I can't help with that. I can answer questions about using Helix Cloud.")
        return _finish(d)

    # 2. greetings, thanks, vague messages: reply directly, no search, no ticket
    reply = small_talk_reply(question)
    if reply is not None:
        d.update(decision="answer", answer=reply,
                 reason="needs_clarification" if reply == CLARIFY else "small_talk")
        return _finish(d)

    # 3. shared daily AI ceiling (all services): stop spending on the LLM, hand over to humans
    if not budget.allow(SERVICE, "answer"):
        d["reason"] = "daily_budget_exhausted"
        return _finish(_escalate(d, question, [], "I can't answer any more questions automatically today."))

    # 4. retrieve, behind deadline + retry budget + circuit breaker
    try:
        kb = clients.search_kb(question, TOP_K)
    except CircuitOpenError:
        d.update(reason="kb_unavailable", degraded=True)
        return _finish(_escalate(d, question, [], "I'm having trouble looking that up right now, and I'd rather not guess."))
    except Exception as exc:
        logger.warning(f"[chat] knowledge search failed: {exc!r}")
        d.update(reason="kb_unavailable", degraded=True)
        return _finish(_escalate(d, question, [], "I'm having trouble looking that up right now, and I'd rather not guess."))

    d["index_version"] = kb.get("index_version")
    d["embedding_model"] = kb.get("embedding_model")
    results = kb.get("results", [])

    # 5. the threshold belongs to the index (version) it was calibrated on
    d["threshold"], d["threshold_key"] = threshold_for(d["index_version"], d["embedding_model"])
    THRESHOLD_LOOKUPS.labels((d["threshold_key"] or "none:").split(":")[0]).inc()
    if d["threshold"] is None:
        d["reason"] = "uncalibrated_index"
        return _finish(_escalate(d, question, [r["doc_id"] for r in results],
                                 "I'm being updated at the moment, and I'd rather not guess."))

    # 6. indirect injection: sanitise every retrieved passage
    passages = []
    for r in results:
        clean, removed = guard.sanitize_document(r["text"])
        if removed:
            logger.info(f"[guard] removed {removed} injected sentence(s) from {r['doc_id']}")
        if clean:
            passages.append({**r, "text": clean})
    retrieved_ids = [p["doc_id"] for p in passages]
    if not passages:
        d["confidence"] = 0.0
        if len(content_terms(question)) <= 1:
            d.update(decision="answer", reason="needs_clarification", answer=CLARIFY)
            return _finish(d)
        return _finish(_escalate(d, question, [], "I don't have an answer for that yet."))

    # 7. confidence vs threshold (retrieval only: deterministic per index version)
    d["confidence"], _coverage = compute_confidence(question, passages[0])
    top = passages[0]
    d["sources"] = [{"doc_id": top["doc_id"], "title": top.get("title"), "source_type": top.get("source_type"),
                     "origin": top.get("origin"), "author": top.get("author"),
                     "embedding_model": top.get("embedding_model")}]
    if d["confidence"] < d["threshold"]:
        # One meaningful word that matches nothing ("hii", "asdf", "billing?") is too little
        # to page an engineer about: ask for detail instead of opening a ticket.
        if len(content_terms(question)) <= 1:
            d.update(decision="answer", reason="needs_clarification", answer=CLARIFY)
            return _finish(d)
        return _finish(_escalate(d, question, retrieved_ids, "I'm not sure I have the right answer for that, and I'd rather not guess."))

    # 8. per-query token budget
    if llm.prompt_tokens(question, top) > MAX_PROMPT_TOKENS:
        d["reason"] = "token_budget_exceeded"
        return _finish(_escalate(d, question, retrieved_ids, "That question needs more detail than I can handle on my own."))

    # 9. grounded answer from the top passage
    gen = llm.generate(question, passages[:1])
    _account(d, gen)
    if guard.looks_like_injection(gen["text"]):
        d["reason"] = "unsafe_output"
        return _finish(_escalate(d, question, retrieved_ids, "I couldn't put together a reliable answer for that."))
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
    budget.record(SERVICE, "answer", gen["model"], gen["prompt_tokens"] + gen["completion_tokens"], d["cost_usd"])
    DAILY_SPEND.set(budget.today()[1])


def _finish(d: dict) -> dict:
    DECISIONS.labels(d["decision"], d["reason"]).inc()
    if d["confidence"] is not None:
        CONFIDENCE.observe(d["confidence"])
    store.save(d)
    if not d.get("dry_run"):
        convs.add(d["conversation_id"], "bot", "helix", d["answer"], d["decision_id"])
        if d["ticket_id"]:
            convs.link_ticket(d["conversation_id"], d["ticket_id"])
    logger.info(f"[decision] {d['decision_id']} {d['decision']}/{d['reason']} conf={d['confidence']} "
          f"thr={d['threshold']} ({d['threshold_key']}) emb={d['embedding_model']} ticket={d['ticket_id']}")
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


# ------------------------------------------------------------------ live hand-off to a human agent

HANDOFFS = Counter("chat_handoffs_total", "Customers who asked to chat with a person")
LIVE_CHATS = Gauge("chat_live_conversations", "Conversations waiting for or talking to an agent")


class MessageIn(BaseModel):
    role: str = Field(pattern="^(user|agent)$")
    author: str = Field(min_length=1, max_length=60)
    text: str = Field(min_length=1, max_length=4000)


class CloseIn(BaseModel):
    agent_id: str = Field(min_length=1, max_length=60)


def _conv_or_404(cid: str) -> dict:
    conv = convs.get(cid)
    if not conv:
        raise HTTPException(404, "conversation not found")
    return conv


def _update_live_gauge() -> None:
    LIVE_CHATS.set(len(convs.with_status(["WAITING_AGENT", "LIVE"])))


class HandoffIn(BaseModel):
    # The ticket of the escalation whose button the customer clicked. Without it the hand-off
    # used the conversation's LATEST ticket, which is the wrong one when two questions in the
    # same conversation escalate close together.
    ticket_id: str | None = None


@app.post("/conversations/{cid}/handoff")
def handoff(cid: str, body: HandoffIn | None = None):
    """Customer chose 'chat with a person'. The ticket already exists and on-call is being paged."""
    conv = _conv_or_404(cid)
    if conv["status"] in ("WAITING_AGENT", "LIVE"):
        return conv
    ticket_id = body.ticket_id if body and body.ticket_id else conv["ticket_id"]
    if not ticket_id:
        raise HTTPException(409, "no ticket for this conversation yet")
    owner = convs.by_ticket(ticket_id)
    if not owner or owner["conversation_id"] != cid:
        raise HTTPException(409, f"{ticket_id} does not belong to this conversation")
    # ticket_id becomes the conversation's current ticket: the agent and the summary use it
    convs.set(cid, status="WAITING_AGENT", agent_id=None, ticket_id=ticket_id)
    convs.add(cid, "system", "helix", f"Connecting you to a support agent (reference {ticket_id}). "
                                      "Please keep this window open; they'll join here shortly.")
    HANDOFFS.inc()
    _update_live_gauge()
    logger.info(f"[handoff] {cid} waiting for an agent ({ticket_id})")
    return convs.get(cid)


class LeaveIn(BaseModel):
    reason: str = "customer"            # customer | resolved


@app.post("/conversations/{cid}/leave")
def leave_queue(cid: str, body: LeaveIn):
    """Stop waiting for an agent and hand the conversation back to the bot.

    Without this, a customer whose agent never joined (or whose ticket was resolved
    from the console form instead) stayed in WAITING_AGENT forever: the bot ignored
    every later message in that conversation. The ticket itself stays open.
    """
    conv = _conv_or_404(cid)
    if conv["status"] != "WAITING_AGENT":
        return conv                     # nothing to do (BOT, LIVE or CLOSED): idempotent
    convs.set(cid, status="BOT")
    text = ("Your request has been resolved by our support team. You can keep asking me questions here."
            if body.reason == "resolved" else
            f"You've stopped waiting for an agent. Your request {conv['ticket_id']} is still open and an "
            "engineer will follow up. You can keep asking me questions here.")
    convs.add(cid, "system", "helix", text)
    _update_live_gauge()
    logger.info(f"[handoff] {cid} left the agent queue ({body.reason})")
    return convs.get(cid)


@app.post("/conversations/{cid}/messages")
def post_message(cid: str, body: MessageIn):
    conv = _conv_or_404(cid)
    if conv["status"] == "CLOSED" and body.role == "agent":
        raise HTTPException(409, "this chat has ended")
    if body.role == "user" and conv["status"] not in ("WAITING_AGENT", "LIVE"):
        raise HTTPException(409, "not in a live chat; send questions to POST /chat")
    if body.role == "agent" and conv["status"] != "LIVE":
        convs.set(cid, status="LIVE", agent_id=body.author)
        convs.add(cid, "system", "helix", f"{body.author} from Helix support joined the chat.")
        _update_live_gauge()
    seq = convs.add(cid, body.role, body.author, body.text.strip())
    return {"seq": seq}


@app.get("/conversations")
def list_conversations(status: str = "WAITING_AGENT,LIVE"):
    return convs.with_status([s.strip().upper() for s in status.split(",") if s.strip()])


@app.get("/conversations/by-ticket/{ticket_id}")
def conversation_for_ticket(ticket_id: str, after: int = 0):
    conv = convs.by_ticket(ticket_id)
    if not conv:
        raise HTTPException(404, "no conversation for this ticket")
    return {**conv, "messages": convs.messages(conv["conversation_id"], after)}


@app.get("/conversations/{cid}")
def get_conversation(cid: str, after: int = 0):
    """The full stored transcript: customer, bot (with why it answered or got stuck), agent."""
    return {**_conv_or_404(cid), "messages": convs.messages(cid, after)}


@app.post("/conversations/{cid}/close")
def close_conversation(cid: str, body: CloseIn):
    """Agent ends the chat. The transcript goes to the Summariser service as an event;
    a draft summary appears in the console's approval list shortly after."""
    conv = _conv_or_404(cid)
    closed = convs.close(cid, body.agent_id, f"{body.agent_id} ended the chat. Thanks for contacting Helix "
                                             "Cloud support! You can keep asking me questions here.")
    _update_live_gauge()
    if closed:
        logger.info(f"[handoff] {cid} closed by {body.agent_id}; conversation.closed queued for the Summariser")
    return {"conversation_id": cid, "ticket_id": conv["ticket_id"], "status": "CLOSED",
            "note": "A draft summary is being prepared and will appear under 'Summaries to approve'."}


@app.get("/config")
def config():
    tokens_today, cost_today = budget.today()
    return {"thresholds": THRESHOLDS, "threshold_strict": THRESHOLD_STRICT, "llm_mode": llm.LLM_MODE,
            "model": llm.OLLAMA_MODEL if llm.LLM_MODE == "ollama" else "simulated",
            "prompt_version": llm.PROMPT_VERSION, "kb_breaker": clients.kb_breaker.state.value,
            "budget": {"max_prompt_tokens": MAX_PROMPT_TOKENS, "daily_token_ceiling": DAILY_TOKEN_CEILING,
                       "daily_cost_ceiling_usd": DAILY_COST_CEILING_USD, "tokens_today": tokens_today,
                       "cost_today_usd": round(cost_today, 6),
                       "shared_with": ["knowledge", "summariser"], "by_service": budget.by_service()},
            "escalations_pending": store.pending_count()}


@app.get("/health")
def health():
    # Chat consumes no events; its own DB holds decisions, conversations and pending escalations
    return health_report(SERVICE, DB_PATH)


@app.get("/", include_in_schema=False)
def ui():
    return FileResponse(Path(__file__).parent / "static" / "index.html", headers=NO_CACHE)
