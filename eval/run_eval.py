"""Calibrate the answer-vs-escalate threshold against the labelled test set.

    python eval/run_eval.py                 # run + print results + threshold sweep
    python eval/run_eval.py --url http://localhost:8001

Uses the Chat service's dry-run mode: the real decision path, but no tickets.

Because the decision is made from retrieval only, each question's confidence
is fixed for a given index version. So one pass is enough: the sweep re-applies
every candidate threshold to the same scores.

Definitions (over questions that reach retrieval, i.e. excluding small talk
and refused injections):
  answer rate        = correctly answered / questions that SHOULD be answered
  wrong-answer rate  = wrongly answered / all retrieval questions
                       (wrong = answered a should-escalate question, or cited the wrong doc)
  false escalations  = should-answer questions that were escalated

Writes eval/results/threshold_curve.csv and threshold_curve.svg, stamped with
the index version and embedding model: a threshold belongs to one model.
"""
import argparse
import csv
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen

HERE = Path(__file__).parent
OUT = HERE / "results"


def ask(base: str, question: str) -> dict:
    req = Request(f"{base}/chat", data=json.dumps({"message": question, "dry_run": True}).encode(),
                  headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=90) as r:
        return json.loads(r.read())


def top_doc(r: dict) -> str | None:
    return r["sources"][0]["doc_id"] if r.get("sources") else None


def sweep(rows: list[tuple[dict, dict]]) -> list[dict]:
    cases = [(c, r) for c, r in rows if c["expected"] in ("answer", "escalate") and r["confidence"] is not None]
    should_answer = sum(1 for c, _ in cases if c["expected"] == "answer")
    out = []
    for i in range(0, 21):
        t = i / 20
        correct = wrong = false_esc = 0
        for c, r in cases:
            answered = r["confidence"] >= t
            if c["expected"] == "answer":
                if answered and top_doc(r) == c["expected_doc"]:
                    correct += 1
                elif answered:
                    wrong += 1
                else:
                    false_esc += 1
            elif answered:
                wrong += 1
        out.append({"threshold": t, "answer_rate": round(correct / max(1, should_answer), 3),
                    "wrong_answer_rate": round(wrong / max(1, len(cases)), 3),
                    "correct_answers": correct, "wrong_answers": wrong, "false_escalations": false_esc,
                    "cases": len(cases)})
    return out


def write_svg(curve: list[dict], path: Path, label: str, chosen: float | None) -> None:
    """Answer rate (y) vs wrong-answer rate (x), one point per threshold. No plotting library needed."""
    W, H, L, B, T, R = 640, 440, 70, 60, 50, 30
    pw, ph = W - L - R, H - T - B
    maxx = max(0.3, max(p["wrong_answer_rate"] for p in curve))
    X = lambda v: L + v / maxx * pw
    Y = lambda v: T + (1 - v) * ph
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" font-family="system-ui, sans-serif" font-size="12">',
             f'<rect width="{W}" height="{H}" fill="#fff"/>',
             f'<text x="{L}" y="24" font-size="15" font-weight="600" fill="#1d2330">Answer rate vs wrong-answer rate as the threshold moves</text>',
             f'<text x="{L}" y="40" fill="#6b7280">{label}</text>']
    for k in range(0, 6):                                      # grid
        v = k / 5
        parts.append(f'<line x1="{L}" x2="{L + pw}" y1="{Y(v)}" y2="{Y(v)}" stroke="#e3e6eb"/>')
        parts.append(f'<text x="{L - 8}" y="{Y(v) + 4}" text-anchor="end" fill="#6b7280">{v:.0%}</text>')
        xv = maxx * k / 5
        parts.append(f'<line x1="{X(xv)}" x2="{X(xv)}" y1="{T}" y2="{T + ph}" stroke="#e3e6eb"/>')
        parts.append(f'<text x="{X(xv)}" y="{T + ph + 18}" text-anchor="middle" fill="#6b7280">{xv:.0%}</text>')
    parts.append(f'<text x="{L + pw / 2}" y="{H - 14}" text-anchor="middle" fill="#1d2330">wrong-answer rate (lower is safer)</text>')
    parts.append(f'<text transform="translate(18,{T + ph / 2}) rotate(-90)" text-anchor="middle" fill="#1d2330">answer rate (higher is more useful)</text>')
    pts = " ".join(f'{X(p["wrong_answer_rate"])},{Y(p["answer_rate"])}' for p in curve)
    parts.append(f'<polyline points="{pts}" fill="none" stroke="#2f5bea" stroke-width="2"/>')
    seen = set()
    for p in curve:
        key = (p["wrong_answer_rate"], p["answer_rate"])
        is_chosen = chosen is not None and abs(p["threshold"] - chosen) < 1e-9
        r = 6 if is_chosen else 3.5
        fill = "#b42318" if is_chosen else "#2f5bea"
        parts.append(f'<circle cx="{X(key[0])}" cy="{Y(key[1])}" r="{r}" fill="{fill}"/>')
        if key not in seen or is_chosen:                       # label each distinct point once
            seen.add(key)
            parts.append(f'<text x="{X(key[0]) + 8}" y="{Y(key[1]) - 6}" fill="{fill}">t={p["threshold"]:.2f}</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8001")
    ap.add_argument("--file", default=str(HERE / "questions.jsonl"))
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

    meta = next((r for _, r in rows if r.get("embedding_model")), {})
    thr, model, index = meta.get("threshold"), meta.get("embedding_model"), meta.get("index_version")
    print(f"\n{correct}/{len(cases)} correct at threshold {thr} (embedding model {model}, index {index})")

    curve = sweep(rows)
    print(f"\nthreshold sweep over {curve[0]['cases']} retrieval questions")
    print("threshold  answer_rate  wrong_answer_rate  false_escalations")
    for p in curve:
        mark = "  <- current" if thr is not None and abs(p["threshold"] - thr) < 1e-9 else ""
        print(f"  {p['threshold']:.2f}       {p['answer_rate']:5.0%}        {p['wrong_answer_rate']:5.0%}"
              f"              {p['false_escalations']:3}{mark}")

    OUT.mkdir(exist_ok=True)
    with open(OUT / "threshold_curve.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(curve[0].keys()) + ["embedding_model", "index_version"])
        w.writeheader()
        for p in curve:
            w.writerow({**p, "embedding_model": model, "index_version": index})
    write_svg(curve, OUT / "threshold_curve.svg",
              f"{len(cases)} labelled questions · embedding model {model} · index {index}", thr)
    print(f"\nwrote {OUT / 'threshold_curve.csv'} and {OUT / 'threshold_curve.svg'}")
    return 0 if correct == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
