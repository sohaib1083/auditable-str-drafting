"""E2-S: the verifier contrast set re-run with the semantic layer switched on.

Same procedure and seed as the notebook's E2 (one corruption per error type per drafted
case, rng seed 7), plus a random sample of clean sentences to measure false alarms.
Usage: SAR_SEMANTIC_MODEL=qwen/qwen3.8-27b python3 e2_semantic.py [n_clean=200]
Writes results/e2s_<model>.csv and results/e2s_<model>_clean.csv.
"""
import collections
import csv
import os
import random
import sys
from pathlib import Path

assert os.environ.get("SAR_SEMANTIC_MODEL"), "set SAR_SEMANTIC_MODEL"
import harness as H  # noqa: E402
import sarbench as B  # noqa: E402

N_CLEAN = int(sys.argv[1]) if len(sys.argv) > 1 else 200
MODEL = os.environ["SAR_SEMANTIC_MODEL"]
RES = Path(__file__).parent / "results"
tag = MODEL.replace("/", "_")


def main():
    os.environ.pop("SAR_SEMANTIC_MODEL")  # off while the offline drafts are produced
    cases = B.build(seed=2026)
    for c in cases:
        H.ingest(c)
    drafted = []
    for c in cases:
        r = H.draft(c["case"]["id"], provider="offline")
        if r["draft"]["status"] == "NEEDS_REVIEW":
            drafted.append(c)
    print(len(drafted), "drafted cases", flush=True)

    rng = random.Random(7)
    rows, clean_pool = [], []
    os.environ["SAR_SEMANTIC_MODEL"] = MODEL  # switch the semantic layer on only for the contrast set
    for c in drafted:
        facts, hyps, outline, raw = H.outline_for(c["case"]["id"])
        allowed = {s["slot"]: s["allowed"] for s in outline["slots"]}
        clean_pool += [(s, facts, hyps, outline) for s in raw]
        for kind in H.ERROR_KINDS:
            cands = raw[:]
            rng.shuffle(cands)
            for s in cands:
                bad = H.corrupt(s, kind, facts, allowed.get(s["slot"], []), rng)
                if bad is None:
                    continue
                v = H.verify_one(bad, facts, hyps, outline)
                outcome = {"failed": "blocked", "flagged": "flagged", "supported": "missed"}[v["status"]]
                codes = sorted({x["code"] for x in v["checks"] if x["status"] != "pass"})
                rows.append({"case": c["case"]["id"], "error": kind, "outcome": outcome, "codes": ",".join(codes),
                             "sem": next((x["message"] for x in v["checks"] if x["code"] == "SEM"), "")})
                break
        print("case", c["case"]["id"], flush=True)
    with open(RES / f"e2s_{tag}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    sample = random.Random(11).sample(clean_pool, min(N_CLEAN, len(clean_pool)))
    crow = []
    for s, facts, hyps, outline in sample:
        v = H.verify_one(s, facts, hyps, outline)
        crow.append({"text": s["text"], "status": v["status"],
                     "sem": next((x["status"] + ": " + x["message"] for x in v["checks"] if x["code"] == "SEM"), "")})
    with open(RES / f"e2s_{tag}_clean.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(crow[0]))
        w.writeheader()
        w.writerows(crow)

    by = collections.defaultdict(collections.Counter)
    for r in rows:
        by[r["error"]][r["outcome"]] += 1
    for k in H.ERROR_KINDS:
        n = sum(by[k].values())
        print(f"{k:24} n={n:3} caught={(by[k]['blocked'] + by[k]['flagged']) / n:.1%}" if n else k)
    fa = collections.Counter(r["status"] for r in crow)
    print(f"clean sample n={len(crow)}: blocked {fa['failed']}, flagged {fa['flagged']}, supported {fa['supported']}")


if __name__ == "__main__":
    main()
