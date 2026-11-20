"""Failure-injection demos. Each one breaks something, shows how the system
degrades, then shows it recover. Run from the repo root with the stack up:

    python scripts/demo.py kb-down          # Knowledge dies -> circuit opens -> escalate, never guess
    python scripts/demo.py kb-slow          # Knowledge slow -> deadline + retry budget -> escalate fast
    python scripts/demo.py tickets-down     # Tickets dies during escalation -> chat outbox delivers later
    python scripts/demo.py broker-down      # RabbitMQ dies -> tickets still accepted, outbox drains later
    python scripts/demo.py oncall-restart   # On-Call restarts mid-timer -> escalation still happens
    python scripts/demo.py duplicate        # replay an event -> consumers ignore the duplicate
    python scripts/demo.py poison           # malformed event -> retried, then dead-lettered; valid events still flow
    python scripts/demo.py loop             # full loop: escalate -> page -> ack -> resolve -> re-ask -> reopen
    python scripts/demo.py all

Watch the Grafana dashboard (http://localhost:3000) while these run.
"""
import json
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CHAT, TICKETS, ONCALL, KB = (f"http://localhost:{p}" for p in (8001, 8002, 8003, 8004))


def call(method: str, url: str, body: dict | None = None, headers: dict | None = None, timeout: float = 30):
    req = Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                  headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except HTTPError as e:
        return {"http_error": e.code, "detail": e.read().decode()[:200]}


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], check=True, capture_output=True)


def wait_healthy(url: str, timeout: float = 60) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urlopen(url + "/health", timeout=2)
            return
        except (URLError, ConnectionError, OSError):
            time.sleep(1)
    raise SystemExit(f"{url} did not come back")


def gap_question() -> str:
    """A question the knowledge base can never have learned: earlier demo runs resolve their
    questions into the KB, so a fixed 'unknown' question stops escalating after the first run."""
    return f"does helix support the orbit-{int(time.time()) % 100000} protocol"


def step(msg: str) -> None:
    print(f"\n>>> {msg}")


def chat(q: str) -> dict:
    t = time.perf_counter()
    r = call("POST", CHAT + "/chat", {"message": q})
    ms = (time.perf_counter() - t) * 1000
    print(f"    {r['decision']:8} reason={r['reason']:22} degraded={str(r['degraded']):5} "
          f"ticket={r.get('ticket_id') or ('queued' if r.get('escalation_pending') else '-'):8} {ms:6.0f} ms")
    return r


# ---------------------------------------------------------------- demos

def kb_down():
    step("Knowledge Base healthy: question answered")
    chat("payment api keeps returning 502 at checkout")
    step("docker compose stop knowledge")
    compose("stop", "knowledge")
    step("Ask 5 times: escalates (never guesses); after 3 failures the circuit OPENS and calls fail fast")
    for _ in range(5):
        chat("payment api keeps returning 502 at checkout")
    print("    breaker:", call("GET", CHAT + "/config")["kb_breaker"])
    step("docker compose start knowledge, wait for the breaker's open timeout (15 s)")
    compose("start", "knowledge")
    wait_healthy(KB)
    time.sleep(16)
    step("Half-open probe succeeds -> circuit CLOSED -> answering again")
    chat("payment api keeps returning 502 at checkout")
    print("    breaker:", call("GET", CHAT + "/config")["kb_breaker"])


def kb_slow():
    step("Inject 5 s latency into Knowledge search (chat's per-attempt deadline is 2 s)")
    call("POST", KB + "/admin/fault", {"latency_ms": 5000, "error_rate": 0})
    for _ in range(4):
        chat("what are the api rate limits per second")
    print("    breaker:", call("GET", CHAT + "/config")["kb_breaker"])
    step("Remove the fault; recovery after the open timeout")
    call("POST", KB + "/admin/fault", {"latency_ms": 0, "error_rate": 0})
    time.sleep(16)
    chat("what are the api rate limits per second")


def tickets_down():
    step("docker compose stop tickets, then ask something the KB cannot answer")
    compose("stop", "tickets")
    r = chat(gap_question())
    print(f"    user sees: {r['answer']}")
    print("    escalations waiting:", call("GET", CHAT + "/config")["escalations_pending"])
    step("docker compose start tickets: the chat outbox worker delivers the escalation")
    compose("start", "tickets")
    wait_healthy(TICKETS)
    for _ in range(30):
        d = call("GET", f"{CHAT}/decisions/{r['decision_id']}")
        if d.get("ticket_id"):
            print(f"    delivered as {d['ticket_id']} after {d['escalation']['attempts']} failed attempt(s); "
                  f"nothing lost, no duplicate (same Idempotency-Key)")
            return
        time.sleep(2)
    print("    not delivered yet; check `docker compose logs chat`")


def broker_down():
    step("docker compose stop rabbitmq, then create 3 tickets")
    compose("stop", "rabbitmq")
    for i in range(3):
        t = call("POST", TICKETS + "/tickets", {"title": f"broker-down demo {i}", "question": "demo"},
                 {"Idempotency-Key": f"broker-demo-{time.time()}-{i}"})
        print(f"    created {t['ticket_id']} (the Ticket Service does not need the broker to accept work)")
    time.sleep(2)
    print("    outbox:", call("GET", TICKETS + "/outbox"))
    step("docker compose start rabbitmq: the relay drains the outbox")
    compose("start", "rabbitmq")
    for _ in range(40):
        o = call("GET", TICKETS + "/outbox")
        if o["pending"] == 0:
            print("    outbox:", o, "-> all events published; On-Call pages for them now")
            return
        time.sleep(2)
    print("    outbox still pending:", call("GET", TICKETS + "/outbox"))


def oncall_restart():
    roster = call("GET", ONCALL + "/roster")["chain"]
    step(f"Escalate a question; On-Call pages {roster[0]}")
    r = call("POST", CHAT + "/chat", {"message": gap_question()})
    tid = r["ticket_id"]
    time.sleep(3)
    print("    pages:", [(p["responder_id"], p["status"]) for p in call("GET", f"{ONCALL}/pages?ticket_id={tid}")])
    step("docker compose restart oncall (mid-timer). Deadlines are in the DB, so the escalation continues")
    compose("restart", "oncall")
    wait_healthy(ONCALL)
    timeout = call("GET", ONCALL + "/console/data")["ack_timeout_seconds"]
    print(f"    waiting for the {timeout:.0f}s ack timeout…")
    time.sleep(timeout + 3)
    print("    pages:", [(p["responder_id"], p["status"]) for p in call("GET", f"{ONCALL}/pages?ticket_id={tid}")])


def duplicate():
    step("Replay the latest incident.resolved and incident.opened events (at-least-once delivery)")
    for et in ("incident.resolved", "incident.opened"):
        out = subprocess.run(["docker", "compose", "exec", "-T", "tickets", "python", "-", et],
                             input=open("scripts/replay_last_event.py", "rb").read(), capture_output=True)
        print("   ", (out.stdout or out.stderr).decode().strip())
    time.sleep(3)
    for svc in ("knowledge", "oncall"):
        logs = subprocess.run(["docker", "compose", "logs", svc, "--tail", "20"], capture_output=True, text=True).stdout
        for line in logs.splitlines():
            if "duplicate" in line:
                print("   ", line.strip())


def dlq_depth() -> int:
    """Messages waiting in the dead-letter queue, read with rabbitmqctl inside the broker container."""
    out = subprocess.run(["docker", "compose", "exec", "-T", "rabbitmq", "rabbitmqctl", "list_queues",
                          "name", "messages", "--quiet"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "support.events.dlq":
            return int(parts[1])
    return -1


def metric(port: int, name: str) -> float:
    """Sum of a counter across its labels, read from a service's /metrics/."""
    try:
        with urlopen(f"http://localhost:{port}/metrics/", timeout=5) as r:
            text = r.read().decode()
    except (URLError, OSError):
        return -1
    return sum(float(line.rsplit(" ", 1)[1]) for line in text.splitlines()
               if line.startswith(name + "{") or line.startswith(name + " "))


def poison():
    before = dlq_depth()
    step(f"Publish a malformed incident.resolved, then a valid event behind it (DLQ depth now {before})")
    out = subprocess.run(["docker", "compose", "exec", "-T", "tickets", "python", "-"],
                         input=open("scripts/publish_poison.py", "rb").read(), capture_output=True)
    text = (out.stdout or out.stderr).decode().strip()
    print("   ", text.replace("\n", "\n    "))
    ids = dict(line.split()[0].split("=") for line in text.splitlines() if "=" in line)
    step("Wait for the valid event, then for the poison message to use up its attempts")
    ok_seen = False
    for _ in range(60):
        logs = subprocess.run(["docker", "compose", "logs", "knowledge", "--since", "3m"], capture_output=True, text=True).stdout
        ok_seen = ok_seen or any(ids.get("valid", "?") in l and "reopened" in l for l in logs.splitlines())
        if dlq_depth() > before and ok_seen:
            break
        time.sleep(1)
    for line in logs.splitlines():
        if ids.get("poison", "?") in line or ids.get("valid", "?") in line or "DEAD-LETTERED" in line:
            print("   ", line.strip()[:220])
    print(f"    valid event processed while the poison message was retried: {ok_seen}")
    print(f"    DLQ depth: {before} -> {dlq_depth()}; knowledge dead_lettered_total = {metric(8004, 'dead_lettered_total')}")


def loop():
    q = "do you offer a student discount"
    step(f"Ask: {q!r}")
    r = chat(q)
    tid = r.get("ticket_id")
    if not tid:
        print("    (answered directly; the KB already knows this. Try `docker compose down -v` for a fresh start)")
        return
    time.sleep(3)
    page = next(p for p in call("GET", f"{ONCALL}/pages?ticket_id={tid}") if p["status"] == "PENDING")
    step(f"{page['responder_id']} acks {page['page_id']}, then resolves with a staff-written summary")
    call("POST", f"{ONCALL}/pages/{page['page_id']}/ack", {"responder_id": page["responder_id"]})
    call("POST", f"{ONCALL}/incidents/{tid}/resolve", {
        "responder_id": page["responder_id"], "summary": "Student discount for the Team plan",
        "resolution": "Students get 50% off the Team plan. Sign up with a university email, then apply under Billing > Discounts."})
    time.sleep(4)
    step("Ask again: answered from the re-indexed resolution")
    r = chat(q)
    print(f"    source: {r['citations']} by {r['sources'][0]['author'] if r['sources'] else '-'}")
    step("The fix was wrong: reopen the ticket -> Knowledge withdraws it -> escalates again")
    call("POST", f"{ONCALL}/incidents/{tid}/reopen", {"responder_id": "carol", "reason": "discount programme has ended"})
    time.sleep(4)
    chat(q)


DEMOS = {"kb-down": kb_down, "kb-slow": kb_slow, "tickets-down": tickets_down, "broker-down": broker_down,
         "oncall-restart": oncall_restart, "duplicate": duplicate, "poison": poison, "loop": loop}

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    if name == "all":
        for fn in DEMOS.values():
            fn()
    elif name in DEMOS:
        DEMOS[name]()
    else:
        print(__doc__)
