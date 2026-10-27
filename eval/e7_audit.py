"""E7: can an independent auditor answer the ten audit questions from the exported records alone?

Usage:
  python3 e7_audit.py <label> [offline|groq] [n_per_family]

For each SAR-Bench-lite case the full product workflow runs on a throw-away database:
  drafted cases   draft -> SAR decision -> a refused edit (adds a tipping-off line) -> a wording edit ->
                  maker review -> submit -> a refused self-approval -> checker approval with attestation ->
                  goAML export -> FIU acknowledgment -> audit pack export by an auditor
  declined cases  draft (the pipeline declines) -> recorded no-file decision -> audit pack export
The pack is then given to auditor.py, which uses no SAR Studio code except the released fact builder,
typology analyst and verifier for the A7 replay. Offline runs also apply 14 tampering mutations to every
drafted pack and record whether the auditor detects each one.

Writes results/e7/<label>/<provider>/{packs,fiu}/, audit.json, mutations.json and summary.json.
Exit code 3 = provider daily limit reached (cached cases are kept; re-run to resume).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.pop("SAR_SEMANTIC_MODEL", None)  # E7 audits the deterministic configuration
LABEL = sys.argv[1] if len(sys.argv) > 1 else "dev"
PROVIDER = sys.argv[2] if len(sys.argv) > 2 else "offline"
N_PER = int(sys.argv[3]) if len(sys.argv) > 3 else 0

import harness as H  # noqa: E402
import sarbench as S  # noqa: E402
import auditor as AU  # noqa: E402
from app import db, main, review  # noqa: E402
from app.config import settings  # noqa: E402
from app.pipeline.facts import build_facts  # noqa: E402
from app.pipeline.gateway import ModelError  # noqa: E402
from app.pipeline.planner import plan  # noqa: E402
from app.pipeline.typology import analyse  # noqa: E402
from app.templates import load_catalogue, load_template  # noqa: E402
from app.verification import verify_sentences  # noqa: E402
from app.verification.claims import VERIFIER_VERSION  # noqa: E402

ROOT = H.EVAL.parent
OUT = H.EVAL / "results" / "e7" / LABEL / PROVIDER
(OUT / "packs").mkdir(parents=True, exist_ok=True)
(OUT / "fiu").mkdir(parents=True, exist_ok=True)

USERS = {"analyst": ("U-AN", "analyst@bench.example", "Analyst A"), "mlro": ("U-ML", "mlro@bench.example", "MLRO B"),
         "auditor": ("U-AU", "auditor@bench.example", "Auditor C")}
U = {role: {"id": i, "email": e, "name": n, "role": role} for role, (i, e, n) in USERS.items()}
with db.session() as conn:
    for u in U.values():
        conn.execute("INSERT OR IGNORE INTO users (id, email, name, role, pw_hash) VALUES (?,?,?,?,?)",
                     (u["id"], u["email"], u["name"], u["role"], "-"))

ATTEST = "I have reviewed every sentence against its cited evidence and approve this report for filing."
BAD_EDIT = " The customer has been informed that this report is being filed."


def sentences(did: str) -> list[dict]:
    conn = db.connect()
    try:
        return db.rows(conn.execute("SELECT * FROM sentences WHERE draft_id=? ORDER BY idx", (did,)).fetchall())
    finally:
        conn.close()


def trail_actions(cid: str, did: str) -> list[str]:
    conn = db.connect()
    try:
        return [r["action"] for r in conn.execute("SELECT action FROM trail WHERE case_id=? AND draft_id=?", (cid, did))]
    finally:
        conn.close()


def run_case(bundle: dict) -> dict:
    """Run the product workflow on one case; return what was attempted and the exported pack."""
    cid = H.ingest(bundle)
    r = H.draft(cid, PROVIDER)
    d = r["draft"]
    did = d["id"]
    if d["status"] == "FAILED":
        if "DAILY_LIMIT" in (d.get("error") or ""):
            raise ModelError(d["error"])
        return {"case": cid, "status": "FAILED", "error": d.get("error")}
    formed = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    log: dict = {"case": cid, "draft": did, "status": d["status"], "family": bundle["_meta"]["family"]}
    if d["status"] == "ABSTAINED":
        review.record_decision(cid, U["analyst"], False, "No report: the system found insufficient grounds and the analyst "
                               "agrees after reviewing the counter-evidence.", formed)
    else:
        review.record_decision(cid, U["analyst"], True, "Activity matches the supported typology; report required.", formed)
        s0 = sentences(did)[0]
        try:
            review.claim_action(did, s0["id"], U["analyst"], "edit", "EDIT_FACTUAL", text=s0["text"] + BAD_EDIT)
            log["bad_edit_refused"] = False
        except review.ReviewError:
            log["bad_edit_refused"] = True
        log["wording_edit"] = False
        for s in sentences(did):
            if " and " in s["text"] and s["verdict"]["status"] == "supported":
                try:
                    review.claim_action(did, s["id"], U["analyst"], "edit", "EDIT_WORDING",
                                        text=s["text"].replace(" and ", " as well as ", 1))
                    log["wording_edit"] = True
                    break
                except review.ReviewError:
                    continue
        review.accept_all_supported(did, U["analyst"])
        for s in sentences(did):
            if s["status"] == "active" and s["review_status"] == "pending":
                review.claim_action(did, s["id"], U["analyst"], "accept", "ACCURATE")
        sub = review.submit(did, U["analyst"])
        try:
            review.approve(did, U["analyst"], ATTEST, sub["content_hash"])
            log["self_approval_refused"] = False
        except review.ReviewError:
            log["self_approval_refused"] = True
        review.approve(did, U["mlro"], ATTEST, sub["content_hash"])
        main.export_goaml(did, U["mlro"])
        review.mark_filed(did, U["mlro"], f"FMU-ACK-{cid.split('-')[-1]}")
        acts = trail_actions(cid, did)
        log["edit_refusal_logged"] = "EDIT_REFUSED" in acts
        log["self_approval_logged"] = "APPROVAL_REFUSED" in acts
        conn = db.connect()
        try:
            payload = conn.execute("SELECT payload FROM filings WHERE draft_id=? ORDER BY created_at DESC LIMIT 1", (did,)).fetchone()[0]
        finally:
            conn.close()
        (OUT / "fiu" / f"{cid}.xml").write_text(payload, encoding="utf-8")
    pack = main._audit_pack(did, U["auditor"], "json")
    (OUT / "packs" / f"{cid}.json").write_text(json.dumps(pack, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    return log


# ── A7 replay with the released code (the only SAR Studio code the auditor relies on) ──

def replay(pack: dict, threshold: float) -> dict:
    evs = sorted(pack["evidence"], key=lambda e: e["seq"])
    ft = build_facts(evs)
    hyps = analyse(ft, threshold)
    tmpl = load_template(pack["case"]["jurisdiction"])
    out = {"versions": {"verifier": VERIFIER_VERSION, "catalogue": load_catalogue()["version"]},
           "fact_hashes": {f["id"]: f["hash"] for f in ft.facts}, "hyp_hashes": {h["id"]: h["hash"] for h in hyps},
           "verdicts": {}, "abstain": None}
    active = [c for c in pack["A1_claims"] if c["status"] == "active"]
    if active and (pack.get("outline") or {}).get("slots"):
        items = [{"slot": c["A2_verdict"].get("slot"), "text": c["text"], "cites": c["cites"]} for c in active]
        for c, v in zip(active, verify_sentences(items, pack["facts"], pack["A8_hypotheses"], pack["outline"]["slots"], tmpl["style"])):
            out["verdicts"][c["id"]] = v["status"]
    if pack["draft"]["status"] == "ABSTAINED":
        out["abstain"] = "abstain" in plan(ft, hyps, tmpl)
    return out


def run_audit(pack: dict, fiu: str | None) -> dict:
    return AU.audit(pack, fiu_copy=fiu, replay=replay, release_root=ROOT)


# ── Tampering mutations on a copy of an exported pack ────────────────────────

def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _ch(v) -> str:
    return _h(json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str))


def _rechain(entries: list[dict], genesis: str, content, ts: str, start: int = 0) -> None:
    prev = entries[start - 1]["entry_hash"] if start else _h(f"genesis:{genesis}")
    for e in entries[start:]:
        e["content_hash"] = _ch(content(e))
        e["prev_hash"] = prev
        e["entry_hash"] = _h(f"{e['content_hash']}|{prev}|{e[ts]}")
        prev = e["entry_hash"]


def _trail_content(e: dict) -> dict:
    return {k: e[k] for k in ("kind", "actor", "action", "draft_id", "detail")}


def _txn(p: dict) -> dict:
    return next(e for e in p["evidence"] if e["type"] == "transaction" and e["id"] in {x for f in p["facts"] for x in f["derived_from"]})


def _claim_with_number(p: dict, rng: random.Random) -> dict:
    act = [c for c in p["A1_claims"] if c["status"] == "active" and any(ch.isdigit() for ch in c["text"])]
    return rng.choice(act)


def _bump_digit(text: str) -> str:
    i = max(i for i, ch in enumerate(text) if ch.isdigit())
    return text[:i] + str((int(text[i]) + 1) % 10) + text[i + 1:]


def m_narrative_text(p, rng):
    c = _claim_with_number(p, rng)
    c["text"] = _bump_digit(c["text"])


def m_citation_swap(p, rng):
    c = rng.choice([c for c in p["A1_claims"] if c["status"] == "active" and any(x.startswith("F") for x in c["cites"])])
    others = [f["id"] for f in p["facts"] if f["id"] not in c["cites"]]
    i = next(i for i, x in enumerate(c["cites"]) if x.startswith("F"))
    c["cites"][i] = rng.choice(others)


def _cited_fact(p, rng):
    cited = {x for c in p["A1_claims"] if c["status"] == "active" for x in c["cites"]}
    return rng.choice([f for f in p["facts"] if f["id"] in cited and any(ch.isdigit() for ch in f["display"])])


def m_fact_edit(p, rng):
    f = _cited_fact(p, rng)
    f["display"] = _bump_digit(f["display"])


def m_fact_edit_rehash(p, rng):
    f = _cited_fact(p, rng)
    f["display"] = _bump_digit(f["display"])
    f["hash"] = _ch({k: v for k, v in f.items() if k not in ("id", "hash")})


def _bump_amount(e: dict) -> None:
    e["content"]["amount"] = e["content"]["amount"] + 1000


def m_evidence_edit(p, rng):
    _bump_amount(_txn(p))


def m_evidence_edit_rehash(p, rng):
    e = _txn(p)
    _bump_amount(e)
    e["content_hash"] = _ch(e["content"])
    e["entry_hash"] = _h(f"{e['content_hash']}|{e['prev_hash']}|{e['captured_at']}")


def m_evidence_rewrite_chain(p, rng):
    e = _txn(p)
    _bump_amount(e)
    evs = sorted(p["evidence"], key=lambda x: x["seq"])
    _rechain(evs, p["case"]["id"], lambda x: x["content"], "captured_at", evs.index(e))


def m_decision_edit(p, rng):
    p["A5_decision"]["reason"] = "Report required: customer is a known fraudster."


def m_trail_delete(p, rng):
    t = p["A3_A4_trail"]
    i = next(i for i, x in enumerate(t) if x["action"] == "CLAIMS_ACCEPTED_BULK")
    del t[i]


def m_trail_edit(p, rng):
    a = next(x for x in p["A3_A4_trail"] if x["action"] == "APPROVED_AND_ATTESTED")
    a["actor"] = U["analyst"]["email"]


def m_endpoint_relabel(p, rng):
    p["draft"]["endpoint"]["model"] = "local-model@in-house"
    p["draft"]["endpoint"]["location"] = "on-premises"


def m_verdict_edit(p, rng):
    c = rng.choice([c for c in p["A1_claims"] if c["status"] == "active"])
    c["A2_verdict"]["status"] = "flagged" if c["A2_verdict"]["status"] == "supported" else "supported"


def m_hypothesis_edit(p, rng):
    h = next(h for h in p["A8_hypotheses"] if h["supported"])
    h["confidence"] = round(h["confidence"] - 0.05, 2)


def m_insider_rewrite(p, rng):
    """An insider edits a sentence and re-signs everything the pack contains consistently."""
    c = _claim_with_number(p, rng)
    old = c["text"]
    c["text"] = _bump_digit(old)
    for h in c.get("history", []):
        if h.get("text") == old:
            h["text"] = c["text"]
    active = [x for x in p["A1_claims"] if x["status"] == "active"]
    fh = _ch([f["hash"] for f in p["facts"]])
    new_hash = _ch({"sentences": [{"id": x["id"], "section": x["section"], "text": x["text"], "cites": x["cites"]} for x in active],
                    "facts": fh})
    old_hash = p["draft"]["content_hash"]
    p["draft"]["content_hash"] = new_hash
    fid = p.get("_fiu_original")
    forged = fid.replace(old, c["text"]) if fid else None
    t = p["A3_A4_trail"]
    first = None
    for i, e in enumerate(t):
        det = e["detail"]
        changed = False
        for k in ("draft_content_hash", "content_hash"):
            if det.get(k) == old_hash:
                det[k] = new_hash
                changed = True
        if e["action"] == "FILING_EXPORTED" and forged is not None:
            det["payload_hash"] = _h(forged)
            changed = True
        if e["action"] == "CLAIM_EDIT" and (det.get("after") or {}).get("text") == old:
            det["after"]["text"] = c["text"]
            changed = True
        if changed and first is None:
            first = i
    for f in p.get("filings", []):
        if forged is not None:
            f["payload_hash"] = _h(forged)
    if first is not None:
        _rechain(t, f"trail:{p['case']['id']}", _trail_content, "at", first)


def _resign(p: dict, old_hash: str, edits: dict) -> None:
    """Re-sign everything after a narrative change: content hash, trail details, filing hash, trail chain."""
    active = [x for x in p["A1_claims"] if x["status"] == "active"]
    fh = _ch([f["hash"] for f in p["facts"]])
    new_hash = _ch({"sentences": [{"id": x["id"], "section": x["section"], "text": x["text"], "cites": x["cites"]} for x in active],
                    "facts": fh})
    p["draft"]["content_hash"] = new_hash
    forged = p.get("_fiu_original")
    for old, new in edits.items():
        forged = forged.replace(old, new) if forged else None
    t = p["A3_A4_trail"]
    first = None
    for i, e in enumerate(t):
        det = e["detail"]
        before = _ch(det)
        for k in ("draft_content_hash", "content_hash"):
            if det.get(k) == old_hash:
                det[k] = new_hash
        if e["action"] == "FILING_EXPORTED" and forged is not None:
            det["payload_hash"] = _h(forged)
        if e["action"] == "CLAIM_EDIT" and (det.get("after") or {}).get("text") in edits:
            det["after"]["text"] = edits[det["after"]["text"]]
        if e["action"] == "CLAIMS_ACCEPTED_BULK":
            det["sentence_ids"] = [x for x in det["sentence_ids"] if x in {c["id"] for c in p["A1_claims"]}]
        if e["action"] == "DRAFT_COMPLETED" and "sentences" in det:
            det["sentences"] = len(active)
        if _ch(det) != before and first is None:
            first = i
    for f in p.get("filings", []):
        if forged is not None:
            f["payload_hash"] = _h(forged)
    if first is not None:
        _rechain(t, f"trail:{p['case']['id']}", _trail_content, "at", first)


def m_insider_delete_sentence(p, rng):
    """An insider deletes a sentence that still verifies, and re-signs every hash the pack contains."""
    act = [c for c in p["A1_claims"] if c["status"] == "active" and c["section"] != "subject"]
    c = rng.choice(act)
    old_hash = p["draft"]["content_hash"]
    p["A1_claims"] = [x for x in p["A1_claims"] if x["id"] != c["id"]]
    _resign(p, old_hash, {c["text"] + " ": "", c["text"]: ""})


def m_insider_hide_filing(p, rng):
    """An insider removes the filing from the end of the trail and restates the draft as approved but not filed."""
    t = p["A3_A4_trail"]
    i = next(i for i, x in enumerate(t) if x["action"] == "FILING_EXPORTED")
    del t[i:]
    p["filings"] = []
    p["draft"]["status"] = "APPROVED"
    p["case"]["status"] = "SAR_APPROVED"


MUTATIONS = [m_narrative_text, m_citation_swap, m_fact_edit, m_fact_edit_rehash, m_evidence_edit, m_evidence_edit_rehash,
             m_evidence_rewrite_chain, m_decision_edit, m_trail_delete, m_trail_edit, m_endpoint_relabel, m_verdict_edit,
             m_hypothesis_edit, m_insider_rewrite, m_insider_delete_sentence,
             m_insider_hide_filing]


def mutate(pack: dict, fn, seed: int, fiu: str | None) -> dict:
    p = copy.deepcopy(pack)
    p["_fiu_original"] = fiu
    fn(p, random.Random(seed))
    p.pop("_fiu_original", None)
    p["pack_hash"] = _ch({k: v for k, v in p.items() if k != "pack_hash"})  # anyone can recompute the pack hash
    return p


def flagged(res: dict) -> list[str]:
    return [k for k, v in res["questions"].items() if v["status"] in ("problem", "unanswerable")]


def main_() -> int:
    cases = S.build()
    if PROVIDER != "offline":
        per: dict = {}
        sel = []
        for c in cases:
            if c["_meta"]["should_file"] and per.get(c["_meta"]["family"], 0) < (N_PER or 5):
                per[c["_meta"]["family"]] = per.get(c["_meta"]["family"], 0) + 1
                sel.append(c)
        cases = sel
    logf = OUT / "runs.json"
    logs = json.loads(logf.read_text()) if logf.exists() else {}
    for b in cases:
        cid = b["case"]["id"]
        if cid in logs and ((OUT / "packs" / f"{cid}.json").exists() or not os.environ.get("E7_RETRY_FAILED")):
            continue  # a failed draft is reported as failed unless E7_RETRY_FAILED is set
        try:
            logs[cid] = run_case(b)
        except ModelError as e:
            if "DAILY_LIMIT" in str(e):
                logf.write_text(json.dumps(logs, indent=1))
                print("daily limit reached; re-run to resume", file=sys.stderr)
                return 3
            raise
        logf.write_text(json.dumps(logs, indent=1))
        print(cid, logs[cid]["status"], flush=True)

    audits = {}
    for cid in logs:
        pf = OUT / "packs" / f"{cid}.json"
        if not pf.exists():
            continue
        pack = json.loads(pf.read_text())
        ff = OUT / "fiu" / f"{cid}.xml"
        fiu = ff.read_text(encoding="utf-8") if ff.exists() else None
        audits[cid] = run_audit(pack, fiu)
        audits[cid]["builtin_check"] = pack["A6_A7_integrity"]["verdict"]  # the system's own Verify, for comparison
    (OUT / "audit.json").write_text(json.dumps(audits, indent=1))

    muts = {}
    if PROVIDER == "offline":
        for n, cid in enumerate(sorted(c for c, a in audits.items() if not a["declined"])):
            pack = json.loads((OUT / "packs" / f"{cid}.json").read_text())
            if pack["pack_version"] == "1.0":  # incomplete packs fail every audit anyway
                break
            fiu = (OUT / "fiu" / f"{cid}.xml").read_text(encoding="utf-8")
            for k, fn in enumerate(MUTATIONS):
                p = mutate(pack, fn, 1000 * n + k, fiu)
                without = AU.audit(p, fiu_copy=None, replay=replay, release_root=ROOT)
                withfiu = AU.audit(p, fiu_copy=fiu, replay=replay, release_root=ROOT)
                muts.setdefault(fn.__name__[2:], []).append(
                    {"case": cid, "verdict": without["verdict"], "flagged": flagged(without),
                     "verdict_with_fiu": withfiu["verdict"], "flagged_with_fiu": flagged(withfiu)})
        (OUT / "mutations.json").write_text(json.dumps(muts, indent=1))

    summary = summarise(logs, audits, muts)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    return 0


def summarise(logs: dict, audits: dict, muts: dict) -> dict:
    out: dict = {"label": LABEL, "provider": PROVIDER, "model": H.gateway.ENDPOINTS[PROVIDER].model,
                 "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    for group, want in (("drafted", False), ("declined", True)):
        a = [x for x in audits.values() if x["declined"] == want]
        if not a:
            continue
        g = {"n": len(a), "pass": sum(x["verdict"] == "PASS" for x in a),
             "builtin_check_pass": sum(x.get("builtin_check") == "PASS" for x in a), "questions": {}}
        for qid in AU.QUESTIONS:
            st = [x["questions"][qid]["status"] for x in a]
            reasons: dict = {}
            for x in a:
                for m in x["questions"][qid]["missing"] + x["questions"][qid]["problems"]:
                    key = m.split(": ", 1)[-1] if m[:1] in "DSFHE" and ": " in m else m
                    reasons[key] = reasons.get(key, 0) + 1
            g["questions"][qid] = {s: st.count(s) for s in ("ok", "problem", "unanswerable", "n/a") if st.count(s)}
            if reasons:
                g["questions"][qid]["reasons"] = dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:4])
        out[group] = g
    dl = [l for l in logs.values() if l.get("status") not in ("ABSTAINED", "FAILED")]
    if dl:
        out["controls"] = {k: sum(bool(l.get(k)) for l in dl) for k in
                           ("bad_edit_refused", "edit_refusal_logged", "self_approval_refused", "self_approval_logged", "wording_edit")}
        out["controls"]["n"] = len(dl)
    out["failed_drafts"] = sum(l.get("status") == "FAILED" for l in logs.values())
    if muts:
        out["mutations"] = {k: {"n": len(v), "detected": sum(r["verdict"] != "PASS" for r in v),
                                "detected_with_fiu": sum(r["verdict_with_fiu"] != "PASS" for r in v),
                                "questions": sorted({q for r in v for q in r["flagged"]})} for k, v in muts.items()}
    return out


if __name__ == "__main__":
    sys.exit(main_())
