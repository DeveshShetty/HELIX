"""Non-determinism test: ask the same questions many times, count decision flips.

    python eval/flip_test.py                      # 20 runs of the questions nearest the threshold
    python eval/flip_test.py --runs 10 --n 4      # fewer calls (useful with LLM_MODE=ollama)

A "flip" = the same question got different decisions (answer vs escalate)
across runs. Because Chat decides from retrieval scores BEFORE generating any
text, the decision should never flip for a fixed index version, even when the
generated wording varies (Ollama). The test reports both, so the claim is
measured, not asserted. Uses dry-run mode: no tickets are opened.
"""
import argparse
import json
from collections import Counter
from pathlib import Path
from urllib.request import Request, urlopen

HERE = Path(__file__).parent


def ask(base: str, q: str) -> dict:
    req = Request(f"{base}/chat", data=json.dumps({"message": q, "dry_run": True}).encode(),
                  headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8001")
    ap.add_argument("--runs", type=int, default=20)
    ap.add_argument("--n", type=int, default=6, help="how many questions closest to the threshold")
    args = ap.parse_args()

    cases = [json.loads(l) for l in (HERE / "questions.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    probe = [(c["question"], ask(args.url, c["question"])) for c in cases]
    probe = [(q, r) for q, r in probe if r["confidence"] is not None and r["threshold"] is not None]
    probe.sort(key=lambda x: abs(x[1]["confidence"] - x[1]["threshold"]))
    chosen = probe[: args.n]
    model = chosen[0][1]["model"] if chosen else "?"

    print(f"{args.runs} runs each of the {len(chosen)} questions closest to the threshold "
          f"(threshold {chosen[0][1]['threshold']}, index {chosen[0][1]['index_version']})\n")
    print(f"{'conf':>5} {'decisions':24} {'flips':>5} {'answer texts':>12}  question")
    total_flipping = 0
    all_models = Counter()
    for q, first in chosen:
        decisions, texts, models = Counter(), Counter(), Counter()
        for _ in range(args.runs):
            r = ask(args.url, q)
            decisions[r["decision"]] += 1
            models[r["model"] or "-"] += 1
            if r["model"]:
                all_models[r["model"]] += 1
            if r["decision"] == "answer":
                texts[r["answer"]] += 1
        flips = len(decisions) > 1
        total_flipping += flips
        dec = ", ".join(f"{k} x{v}" for k, v in decisions.items())
        print(f"{first['confidence']:5.2f} {dec:24} {'YES' if flips else 'no':>5} {len(texts):>12}  {q}")
    print(f"\n{total_flipping}/{len(chosen)} questions flipped decision across {args.runs} runs "
          f"(LLM: {', '.join(sorted(all_models)) or 'not called: every decision escalated'}).")
    print("'answer texts' = distinct wordings of the answer: >1 means the generated text varied.")


if __name__ == "__main__":
    main()
