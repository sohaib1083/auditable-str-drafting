"""E2-S held-out: the revised semantic judge (v2) on data not used to revise it.

- 200 clean sentences sampled with a different seed, excluding every sentence in the
  development sample (results/e2s_<model>_clean.csv) that was inspected to revise the prompt;
- the 80 original unsupported-claim corruptions (same procedure and seed as E2);
- 80 corruptions using four NEW unsupported claims never seen during development.
Usage: SAR_SEMANTIC_MODEL=qwen/qwen3.8-27b python3 e2s_heldout.py
"""
import collections
import csv
import hashlib
import json
import os
import random
import sys
from pathlib import Path

MODEL = os.environ.pop("SAR_SEMANTIC_MODEL")  # off while offline drafts are produced
import harness as H  # noqa: E402
import sarbench as B  # noqa: E402
from app.verification import semantic  # noqa: E402

RES = Path(__file__).parent / "results"
tag = MODEL.replace("/", "_")
HELDOUT_CLAIMS = ["The subject recently travelled to Dubai twice before the deposits began.",
                  "The account was previously closed by another bank over similar concerns.",
                  "The employer confirmed that the subject had been dismissed last year.",
                  "Part of the cash came from the sale of a family vehicle."]


CACHE = Path(__file__).parent / "results" / "e2s_v2_cache"
CACHE.mkdir(parents=True, exist_ok=True)


def judged(s, facts, hyps, outline):
    """Verify one sentence, cached; exit 3 if the judge is unavailable (daily limit) so the run can resume later."""
    f = CACHE / (hashlib.sha256(json.dumps([s["text"], s.get("cites")]).encode()).hexdigest()[:24] + ".json")
    if f.exists():
        return json.load(open(f))
    v = H.verify_one(s, facts, hyps, outline)
    sem = next((x for x in v["checks"] if x["code"] == "SEM"), None)
    if sem and sem["status"] == "warn":
        print("judge unavailable (daily limit); resume later", flush=True)
        sys.exit(3)
    rec = {"status": v["status"], "sem_status": sem["status"] if sem else "not-run", "sem": sem["message"] if sem else "",
           "codes": sorted({x["code"] for x in v["checks"] if x["status"] != "pass"})}
    json.dump(rec, open(f, "w"))
    return rec


def main():
    dev = {r["text"] for r in csv.DictReader(open(RES / f"e2s_{tag}_clean.csv"))}
    cases = B.build(seed=2026)
    drafted = []
    for c in cases:
        H.ingest(c)
        if H.draft(c["case"]["id"], provider="offline")["draft"]["status"] == "NEEDS_REVIEW":
            drafted.append(c)
    os.environ["SAR_SEMANTIC_MODEL"] = MODEL
    rng, rng_h = random.Random(7), random.Random(13)
    sem, pool = [], []
    for c in drafted:
        facts, hyps, outline, raw = H.outline_for(c["case"]["id"])
        allowed = {s["slot"]: s["allowed"] for s in outline["slots"]}
        pool += [(s, facts, hyps, outline) for s in raw if s["text"] not in dev]
        for which, rr in (("original", rng), ("heldout", rng_h)):
            cands = raw[:]
            rr.shuffle(cands)
            s = cands[0]
            bad = H.corrupt(s, "SEM_unsupported_claim", facts, allowed.get(s["slot"], []), rr)
            if which == "heldout":
                bad["text"] = s["text"].rstrip(".") + ". " + rr.choice(HELDOUT_CLAIMS)
            v = judged(bad, facts, hyps, outline)
            sem.append({"set": which, "case": c["case"]["id"], "status": v["status"], "sem_status": v["sem_status"],
                        "codes": ",".join(v["codes"]), "text": bad["text"]})
    sample = random.Random(23).sample(pool, min(200, len(pool)))
    clean = []
    for s, facts, hyps, outline in sample:
        v = judged(s, facts, hyps, outline)
        clean.append({"text": s["text"], "status": v["status"], "sem_status": v["sem_status"], "sem": v["sem"]})
    for name, rows in ((f"e2s_v{semantic.SEMANTIC_VERSION}_{tag}_sem.csv", sem), (f"e2s_v{semantic.SEMANTIC_VERSION}_{tag}_clean.csv", clean)):
        with open(RES / name, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    by = collections.defaultdict(collections.Counter)
    for r in sem:
        by[r["set"]][r["status"]] += 1
    for k, cnt in by.items():
        n = sum(cnt.values())
        print(f"SEM {k}: n={n} caught={(cnt['failed'] + cnt['flagged']) / n:.1%}")
    fa = collections.Counter(r["status"] for r in clean)
    print(f"held-out clean n={len(clean)}: blocked {fa['failed']}, flagged {fa['flagged']}, supported {fa['supported']}")


if __name__ == "__main__":
    main()
