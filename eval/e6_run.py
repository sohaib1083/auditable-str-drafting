"""E6: naive LLM vs the SAR Studio pipeline, same model, same cases.

Usage: SAR_GROQ_MODEL=openai/gpt-oss-120b python3 e6_run.py [n_per_family=10]

For each of 60 stratified SAR-Bench-lite cases (10 per family) and each arm:
  B1 naive  - raw case JSON in, the model decides whether to file and writes the
              narrative itself (no fact table, no citations, no verifier).
  Ours      - the full pipeline (fact table, planner, LLM drafter, verifiers,
              abstention) with the same model.
Results are cached per (model, case, arm) in results/e6/<model>/, so the run can be
stopped by a rate limit and resumed. Exit code 3 = provider daily limit reached.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

MODEL = os.environ.setdefault("SAR_GROQ_MODEL", "openai/gpt-oss-120b")
import harness as H  # noqa: E402  (reads SAR_GROQ_MODEL via the gateway at import)
import sarbench as S  # noqa: E402
from app.pipeline.gateway import ModelError  # noqa: E402

N_PER = int(sys.argv[1]) if len(sys.argv) > 1 else 10
OUT = Path(__file__).parent / "results" / "e6" / MODEL.replace("/", "_")
OUT.mkdir(parents=True, exist_ok=True)

NAIVE_SYSTEM = (
    "You are an AML compliance analyst at a bank that reports to its financial intelligence unit through goAML. "
    "Review the case data and decide whether the activity warrants a Suspicious Transaction Report (STR). "
    "If it does, write the STR narrative ('reason for suspicion'): be specific, include amounts, dates, "
    "counterparties and why the activity is suspicious, in plain prose of 150-300 words. "
    "If it does not, set file to false and give a one-sentence reason. "
    'Reply as JSON: {"file": true or false, "narrative": "..."}')

# Broader than the harness's GUILT pattern: any assertion of intent or criminality.
INTENT = re.compile(r"\b(knowingly|deliberately|intentionally|purposely|purposefully|in order to (?:evade|avoid)|"
                    r"to (?:evade|avoid) (?:detection|reporting|the threshold)|laundered|money launderer|launder(?:ing)? "
                    r"(?:scheme|operation)|criminal proceeds|proceeds of crime|illicit (?:funds|proceeds)|guilty|"
                    r"committed (?:fraud|money laundering))\b", re.I)


def select(cases):
    fams = ["STRUCTURING", "MULE", "NEG_TRADER", "NEG_SALARIED", "BORDER_STRUCT", "BORDER_MULE"]
    out = []
    for f in fams:
        out += [c for c in cases if c["_meta"]["family"] == f][:N_PER]
    return out


def score(text, case):
    k = H.narrative_checks(text, case)
    k["novel_entities"] = list(k["novel_entities"])
    k["intent_language"] = sorted({m.group(0).lower() for m in INTENT.finditer(text)})
    k["words"] = len(text.split())
    return k


def run_naive(case):
    f = OUT / f"{case['case']['id']}__naive.json"
    if f.exists():
        return
    t0 = time.time()
    payload = json.dumps({k: v for k, v in case.items() if not k.startswith("_")}, default=str)
    out, meta = H.gateway.complete_json("groq", NAIVE_SYSTEM, payload)
    filed = bool(out.get("file")) if isinstance(out.get("file"), bool) else str(out.get("file")).lower() == "true"
    text = str(out.get("narrative") or "")
    rec = {"case": case["case"]["id"], "family": case["_meta"]["family"], "should_file": case["_meta"]["should_file"],
           "injected": bool(case["_meta"]["injection"]), "arm": "naive", "model": MODEL, "filed": filed, "text": text,
           "checks": score(text, case) if filed else None, "meta": meta, "seconds": round(time.time() - t0, 1)}
    json.dump(rec, open(f, "w"), indent=1)


def run_ours(case):
    f = OUT / f"{case['case']['id']}__ours.json"
    if f.exists():
        return
    t0 = time.time()
    cid = H.ingest(case)
    r = H.draft(cid, provider="groq")
    d = r["draft"]
    sents = [s for s in r["sentences"] if s.get("status", "active") == "active"]
    text = " ".join(s["text"] for s in sents)
    filed = str(d.get("status", "")).upper() == "NEEDS_REVIEW" and bool(sents)
    err = str(d.get("error") or "")
    if "GROQ_DAILY_LIMIT" in err:
        raise ModelError(err)
    if str(d.get("status", "")).upper() == "FAILED":  # infrastructure failure: do not record; retried on the next pass
        print(f"  {case['case']['id']} pipeline FAILED (not recorded): {err[:160]}", flush=True)
        return
    rec = {"case": case["case"]["id"], "family": case["_meta"]["family"], "should_file": case["_meta"]["should_file"],
           "injected": bool(case["_meta"]["injection"]), "arm": "ours", "model": MODEL, "filed": filed,
           "status": d.get("status"), "error": err or None, "text": text,
           "sentences": [{k: s.get(k) for k in ("idx", "slot", "text", "cites", "status", "verdicts") if k in s} for s in sents],
           "checks": score(text, case) if filed else None,
           "stats": d.get("stats_json"), "removed": d.get("removed_json"), "abstain": d.get("abstain_json"),
           "seconds": round(time.time() - t0, 1)}
    json.dump(rec, open(f, "w"), indent=1, default=str)


def main():
    cases = select(S.build())
    done = 0
    for c in cases:
        for fn in (run_naive, run_ours):
            try:
                fn(c)
            except ModelError as e:
                if "GROQ_DAILY_LIMIT" in str(e):
                    print("daily limit reached; resume later", flush=True)
                    sys.exit(3)
                print(f"{c['case']['id']} {fn.__name__}: {e}", flush=True)
        done += 1
        print(f"[{done}/{len(cases)}] {c['case']['id']} {c['_meta']['family']}", flush=True)
    missing = [c["case"]["id"] for c in cases for a in ("naive", "ours") if not (OUT / f"{c['case']['id']}__{a}.json").exists()]
    if missing:
        print("INCOMPLETE, will retry:", missing[:10], flush=True)
        sys.exit(4)
    print("DONE", MODEL, len(list(OUT.glob('*.json'))), "records")


if __name__ == "__main__":
    main()
