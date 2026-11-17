"""Run the labelled questions against the Chat service and report accuracy.

    python eval/run_eval.py                    # against http://localhost:8001
    python eval/run_eval.py --sweep            # also show what each threshold WOULD do

The sweep re-applies different thresholds to the confidence scores the
service returned, so one run shows the answer-vs-escalate trade-off curve:
  false answer   = answered when it should have escalated  (risk: hallucination)
  false escalate = escalated when it should have answered  (cost: on-call noise)
"""
import argparse
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen

HERE = Path(__file__).parent


def ask(base: str, question: str) -> dict:
    req = Request(f"{base}/chat", data=json.dumps({"message": question}).encode(),
                  headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8001")
    ap.add_argument("--file", default=str(HERE / "questions.jsonl"))
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()

    cases = [json.loads(l) for l in Path(args.file).read_text(encoding="utf-8").splitlines() if l.strip()]
    rows, correct = [], 0
    print(f"{'expected':9} {'got':9} {'conf':>5}  {'category':10} question")
    for c in cases:
        r = ask(args.url, c["question"])
        ok = r["decision"] == c["expected"]
        if ok and c["expected"] == "answer" and c.get("expected_doc"):
            ok = c["expected_doc"] in r["citations"]
        correct += ok
        conf = "" if r["confidence"] is None else f"{r['confidence']:.2f}"
        print(f"{c['expected']:9} {r['decision']:9} {conf:>5}  {c['category']:10} {'' if ok else 'X '}{c['question']}")
        rows.append((c, r))

    print(f"\n{correct}/{len(cases)} correct at threshold {rows[0][1]['threshold'] if rows else '?'}")

    if args.sweep:
        scored = [(c, r) for c, r in rows if r["confidence"] is not None
                  and c["expected"] in ("answer", "escalate") and r["reason"] != "kb_unavailable"]
        print(f"\nthreshold sweep over {len(scored)} answer/escalate cases")
        print("threshold  false_answer  false_escalate")
        for t in [x / 20 for x in range(4, 17)]:
            fa = sum(1 for c, r in scored if c["expected"] == "escalate" and r["confidence"] >= t)
            fe = sum(1 for c, r in scored if c["expected"] == "answer" and r["confidence"] < t)
            print(f"  {t:.2f}       {fa:3}           {fe:3}")
    return 0 if correct == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
