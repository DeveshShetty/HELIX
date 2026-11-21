"""Closed-loop load test for POST /chat (stdlib only).

    python scripts/load_test.py --users 20 --seconds 60
    python scripts/load_test.py --steps 5,10,20,40 --seconds 30     # find where it degrades

Each user sends a question, waits for the answer, then sends the next. Questions mix
KB hits (answered) and unknown ones (escalate -> Tickets -> outbox). Reports
throughput, p50/p95/p99 latency and error rate per step.
"""
import argparse
import json
import random
import statistics
import threading
import time
import urllib.error
import urllib.request

URL = "http://localhost:8001/chat"
QUESTIONS = ["how do I reset my password", "how do I export my data", "how do I enable two-factor authentication",
             "how do I cancel my subscription", "how do I change my billing email", "how do I invite a teammate"]


def one(q: str) -> tuple[float, bool, str]:
    body = json.dumps({"message": q}).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
            return time.perf_counter() - t, True, f"{d.get('decision', '?')}/{d.get('reason', '?')}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return time.perf_counter() - t, False, f"{type(e).__name__}{getattr(e, 'code', '')}"


def run(users: int, seconds: float, unknown_share: float) -> dict:
    results, lock, stop = [], threading.Lock(), time.monotonic() + seconds

    def worker():
        while time.monotonic() < stop:
            q = (f"does helix support the orbit-{random.randint(0, 99999)} protocol"
                 if random.random() < unknown_share else random.choice(QUESTIONS))
            r = one(q)
            with lock:
                results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(users)]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.monotonic() - started
    lat = sorted(r[0] * 1000 for r in results)
    errors = sum(1 for r in results if not r[1])
    q = statistics.quantiles(lat, n=100) if len(lat) >= 2 else [lat[0]] * 99
    decisions = {}
    for r in results:
        decisions[r[2]] = decisions.get(r[2], 0) + 1
    return {"users": users, "requests": len(results), "rps": round(len(results) / wall, 1),
            "p50_ms": round(q[49]), "p95_ms": round(q[94]), "p99_ms": round(q[98]), "max_ms": round(lat[-1]),
            "error_rate": round(errors / len(results), 4), "outcomes": decisions}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=20)
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--steps", default="")
    ap.add_argument("--unknown-share", type=float, default=0.2)
    a = ap.parse_args()
    for u in ([int(x) for x in a.steps.split(",")] if a.steps else [a.users]):
        print(json.dumps(run(u, a.seconds, a.unknown_share)), flush=True)
