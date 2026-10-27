"""Independent auditor for SAR Studio audit packs (experiment E7).

Answers the ten audit questions of the paper (Table 1) from one exported audit
pack, the way an examiner would: from the records alone, without access to the
bank's database or to the analyst.

Independence: this file imports only the standard library. Hash chains,
content hashes, fact and hypothesis hashes and the attested draft hash are all
recomputed here from the construction stated in the paper, not by calling
SAR Studio code. The one exception is A7 (reproducibility), which by
definition re-runs the released fact builder, typology analyst and verifier;
the caller passes that in as `replay`, and this module only compares outputs.

Each question gets one of four statuses:
  ok            the records answer the question and show no problem
  problem       the records answer it and show a problem (tampering, gap, mismatch)
  unanswerable  the records needed to answer it are missing from the pack
  n/a           the question does not apply (e.g. no narrative for a declined case)
The pack passes only if every applicable question is ok.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

QUESTIONS = {
    "A1": "Where did this number, date or name come from?",
    "A2": "Was that link checked, how, and with what result?",
    "A3": "Which model, prompt and parameters produced this sentence?",
    "A4": "What did the human reviewer see, change and approve?",
    "A5": "Who decided to file (or not to file), and when?",
    "A6": "Has any record been altered since approval?",
    "A7": "Can the verification be reproduced today?",
    "A8": "Was the typology supported, and was counter-evidence considered?",
    "A9": "Did confidential data leave the approved boundary?",
    "A10": "How does the system perform overall?",
}
MAKER_ACTIONS = {"CLAIM_ACCEPT", "CLAIM_EDIT", "CLAIM_REJECT", "CLAIMS_ACCEPTED_BULK", "SUBMITTED_FOR_APPROVAL"}


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _canon(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _ch(v: Any) -> str:
    return _h(_canon(v))


def _walk(entries: list[dict[str, Any]], genesis: str, content: Callable[[dict], Any], ts: str) -> list[str]:
    """Recompute a hash chain; return a list of problems (empty if intact)."""
    out, prev = [], _h(f"genesis:{genesis}")
    for n, e in enumerate(entries, 1):
        tag = e.get("id") or f"seq {e['seq']}"
        if e["seq"] != n:
            out.append(f"{tag}: sequence {e['seq']} where {n} was expected (entry missing or reordered)")
        if _ch(content(e)) != e["content_hash"]:
            out.append(f"{tag}: content does not match its recorded hash")
        if e["prev_hash"] != prev:
            out.append(f"{tag}: does not link to the previous entry")
        if _h(f"{e['content_hash']}|{e['prev_hash']}|{e[ts]}") != e["entry_hash"]:
            out.append(f"{tag}: entry hash does not match its own fields")
        prev = e["entry_hash"]
    return out


class _Q:
    def __init__(self) -> None:
        self.problems: list[str] = []
        self.missing: list[str] = []
        self.na = False

    def status(self) -> str:
        if self.na:
            return "n/a"
        return "problem" if self.problems else "unanswerable" if self.missing else "ok"


def audit(pack: dict[str, Any], fiu_copy: str | None = None, replay: Callable[[dict], dict] | None = None,
          release_root: Path | None = None) -> dict[str, Any]:
    q = {k: _Q() for k in QUESTIONS}
    case_id = pack["case"]["id"]
    d = pack["draft"]
    did = d["id"]
    claims = pack.get("A1_claims") or []
    active = [c for c in claims if c["status"] == "active"]
    facts = {f["id"]: f for f in pack.get("facts") or []}
    hyps = {h["id"]: h for h in pack.get("A8_hypotheses") or []}
    ev = {e["id"]: e for e in pack.get("evidence") or []}
    trail = pack.get("A3_A4_trail") or []
    mine = [t for t in trail if t["draft_id"] == did]
    runs: dict[str, list[dict]] = {}
    for t in mine:
        if t["action"] == "AGENT_RUN":
            runs.setdefault(t["detail"].get("agent"), []).append(t)
    first = {a: r[0]["detail"] for a, r in runs.items()}
    declined = d["status"] == "ABSTAINED"
    endpoint = d.get("endpoint") or {}
    hosted = endpoint.get("provider") not in (None, "offline")

    # Shared recomputations
    fb = first.get("fact_builder", {})
    facts_hash_recorded = fb.get("output_hash")
    facts_in_order = pack.get("facts") or []
    facts_hash_pack = _ch([f["hash"] for f in facts_in_order])
    facts_complete = facts_hash_recorded is not None and facts_hash_pack == facts_hash_recorded
    evs = sorted(pack.get("evidence") or [], key=lambda e: e["seq"])
    ev_contiguous = [e["seq"] for e in evs] == list(range(1, len(evs) + 1))
    ev_declared_full = pack.get("evidence_scope") == "full"

    def fact_ok(fid: str, qq: _Q) -> None:
        f = facts.get(fid)
        if f is None:
            (qq.problems if pack.get("facts_scope") == "full" else qq.missing).append(f"{fid} is cited but not in the pack")
            return
        if _ch({k: v for k, v in f.items() if k not in ("id", "hash")}) != f["hash"]:
            qq.problems.append(f"{fid}: fact content does not match its hash")
        for e in f["derived_from"]:
            if e not in ev:
                (qq.problems if ev_declared_full else qq.missing).append(f"{fid}: source {e} not in the pack")
            elif _ch(ev[e]["content"]) != ev[e]["content_hash"]:
                qq.problems.append(f"{fid}: source {e} content does not match its hash")

    def hyp_ok(hid: str, qq: _Q) -> None:
        h = hyps.get(hid)
        if h is None:
            qq.problems.append(f"{hid} is cited but not in the pack")
            return
        if _ch({k: v for k, v in h.items() if k not in ("id", "hash")}) != h["hash"]:
            qq.problems.append(f"{hid}: hypothesis content does not match its hash")
        for fid in h["support_fact_ids"] + h["counter_fact_ids"]:
            fact_ok(fid, qq)

    # ── A1 provenance ──────────────────────────────────────────────────────
    if declined:
        ab = pack.get("abstain")
        if not ab:
            q["A1"].missing.append("the reasons for declining are not in the pack")
        else:
            for h in ab.get("hypotheses", []):
                for fid in h.get("counter_fact_ids", []):
                    fact_ok(fid, q["A1"])
        for hid in hyps:
            hyp_ok(hid, q["A1"])
        if not hyps:
            q["A1"].missing.append("no typology hypotheses in the pack")
    else:
        for c in active:
            if not c["cites"]:
                q["A1"].problems.append(f"{c['id']} has no citation")
            for cid in c["cites"]:
                (hyp_ok if cid.startswith("H") else fact_ok)(cid, q["A1"])

    # ── A2 verification record ─────────────────────────────────────────────
    if declined:
        q["A2"].na = True
    else:
        for c in active:
            v = c.get("A2_verdict") or {}
            if not v.get("checks") or not v.get("verifier"):
                q["A2"].missing.append(f"{c['id']}: no stored verdict with checks and verifier version")
            elif v["status"] not in ("supported", "flagged"):
                q["A2"].problems.append(f"{c['id']}: active sentence has verdict {v['status']}")
            elif v["status"] == "flagged" and not any(x["status"] != "pass" for x in v["checks"]):
                q["A2"].problems.append(f"{c['id']}: flagged without a non-passing check")
            elif v["status"] == "supported" and any(x["status"] == "fail" for x in v["checks"]):
                q["A2"].problems.append(f"{c['id']}: marked supported despite a failed check")
            if any(x["code"] == "SEM" for x in v.get("checks", [])) and not any(
                    t["detail"].get("semantic_endpoint") for t in runs.get("verifier", [])):
                q["A2"].missing.append(f"{c['id']}: semantic judge used but its endpoint is not recorded")
        for r in pack.get("removed_by_verifier") or []:
            if not r.get("problems"):
                q["A2"].missing.append("a removed sentence has no recorded reason")
        done = [t["detail"] for t in mine if t["action"] == "DRAFT_COMPLETED"]
        if done and done[-1].get("removed") != len(pack.get("removed_by_verifier") or []):
            q["A2"].problems.append("sentences removed by the verifier differ from the number recorded in the trail")

    # ── A3 generation record ───────────────────────────────────────────────
    if declined:
        q["A3"].na = True
    else:
        dr = first.get("drafter")
        if not dr:
            q["A3"].missing.append("no drafter run in the trail")
        else:
            for k in ("endpoint_id", "model", "prompt_template", "input_hash", "output_hash"):
                if not dr.get(k):
                    q["A3"].missing.append(f"drafter run does not record {k}")
            if dr.get("endpoint_id") != endpoint.get("id") or dr.get("model") != endpoint.get("model"):
                q["A3"].problems.append("draft header and drafter run disagree on endpoint or model")
            if dr.get("input_hash") and facts_hash_recorded and dr["input_hash"] != facts_hash_recorded:
                q["A3"].problems.append("drafter input is not the recorded fact table")
            if not facts_complete:
                (q["A3"].problems if pack.get("facts_scope") == "full" else q["A3"].missing).append(
                    "the fact table in the pack does not reproduce the recorded fact-table hash, so the drafter's input cannot be confirmed")
            if hosted:
                calls = dr.get("model_calls")
                if not calls:
                    q["A3"].missing.append("model call record (exact request hash and parameters) not stored")
                else:
                    for cl in calls:
                        if not cl.get("request_hash") or not cl.get("params"):
                            q["A3"].missing.append("a model call lacks its request hash or parameters")
                loops = {t["detail"].get("loop"): t["detail"] for t in runs.get("verifier", [])}
                if any("repair_calls" not in v for v in loops.values()):
                    q["A3"].missing.append("model calls made to repair failed sentences are not recorded")
                for c in active:
                    for hist in c.get("history", []):
                        if hist["event"].startswith("repaired"):
                            n = int(hist["event"].split("loop ")[1].rstrip(")"))
                            if not (loops.get(n) or {}).get("repair_calls"):
                                q["A3"].missing.append(f"{c['id']}: repaired in loop {n}, but the repair call is not recorded")
        for c in active:
            h = c.get("history") or []
            if not h or h[0]["event"] != "drafted":
                q["A3"].problems.append(f"{c['id']}: origin of the sentence not recorded")

    # ── A4 human review ────────────────────────────────────────────────────
    if declined:
        q["A4"].na = True
    else:
        reviewed: set[str] = set()
        edits = {}
        for t in mine:
            if t["action"] == "CLAIMS_ACCEPTED_BULK":
                reviewed |= set(t["detail"]["sentence_ids"])
            elif t["action"] in ("CLAIM_ACCEPT", "CLAIM_EDIT", "CLAIM_REJECT"):
                reviewed.add(t["detail"]["sentence_id"])
                if t["action"] == "CLAIM_EDIT":
                    edits[t["detail"]["sentence_id"]] = t["detail"]
        for c in claims:
            if c["status"] in ("active", "rejected") and c["id"] not in reviewed:
                q["A4"].problems.append(f"{c['id']}: no recorded review action")
            if c["review"] == "edited":
                e = edits.get(c["id"])
                if not e or "before" not in e or (e.get("after") or {}).get("text") != c["text"]:
                    q["A4"].problems.append(f"{c['id']}: edit not recorded with before and after text")
        makers = {t["actor"] for t in mine if t["action"] in MAKER_ACTIONS}
        sub = [t for t in mine if t["action"] == "SUBMITTED_FOR_APPROVAL"]
        app = [t for t in mine if t["action"] == "APPROVED_AND_ATTESTED"]
        if not sub or not app:
            q["A4"].missing.append("submission or approval not in the trail")
        else:
            a = app[-1]
            if len((a["detail"].get("attestation") or "").strip()) < 20:
                q["A4"].problems.append("approval has no attestation statement")
            if a["actor"] in makers:
                q["A4"].problems.append("approver also acted as maker (segregation of duties)")
            body = [{"id": c["id"], "section": c["section"], "text": c["text"], "cites": c["cites"]} for c in active]
            fh = facts_hash_pack if facts_complete else facts_hash_recorded
            recomputed = _ch({"sentences": body, "facts": fh})
            if recomputed != a["detail"].get("draft_content_hash"):
                q["A4"].problems.append("the narrative in the pack is not the one the approver attested")
            if recomputed != d.get("content_hash"):
                q["A4"].problems.append("the narrative does not match the draft's recorded content hash")

    # ── A5 decision ────────────────────────────────────────────────────────
    dec = pack.get("A5_decision")
    dec_entries = [t for t in trail if t["action"] == "SAR_DECISION_RECORDED"]
    if not dec or not dec_entries:
        q["A5"].missing.append("no decision record")
    else:
        t = dec_entries[-1]
        for k in ("reason", "decided_at", "suspicion_formed_at", "deadline_at"):
            if not dec.get(k):
                q["A5"].missing.append(f"decision lacks {k}")
        det = t["detail"]
        same = (det.get("decision_id") == dec.get("id") and det.get("reason") == dec.get("reason")
                and bool(det.get("sar_required")) == bool(dec.get("sar_required"))
                and det.get("deadline_at") == dec.get("deadline_at") and det.get("suspicion_formed_at") == dec.get("suspicion_formed_at"))
        if not same:
            q["A5"].problems.append("decision record differs from the chained trail entry")
        actors = pack.get("actors")
        if actors is not None:
            who = actors.get(t["actor"]) or {}
            if not who or f"{who.get('name')} ({who.get('role')})" != dec.get("decided_by"):
                q["A5"].problems.append("named decision-maker differs from the chained trail actor")
        if declined and dec.get("sar_required"):
            q["A5"].problems.append("system declined but the decision record says file, with no draft to file")
        if not declined:
            if not dec.get("sar_required"):
                q["A5"].problems.append("narrative approved without a decision to file")
            if d["status"] == "FILED" and not any(t2["action"] == "FILED_WITH_FIU" for t2 in mine):
                q["A5"].problems.append("draft is marked filed but no filing with the FIU is recorded")
            sub_seq = min((s["seq"] for s in mine if s["action"] == "SUBMITTED_FOR_APPROVAL"), default=None)
            if sub_seq is not None and t["seq"] > sub_seq:
                q["A5"].problems.append("decision recorded after submission")

    # ── A6 integrity ───────────────────────────────────────────────────────
    q["A6"].problems += _walk(trail, f"trail:{case_id}", lambda e: {k: e[k] for k in ("kind", "actor", "action", "draft_id", "detail")}, "at")
    if not evs:
        q["A6"].missing.append("no evidence in the pack")
    elif not ev_contiguous:
        gaps = sorted(set(range(1, max(e["seq"] for e in evs) + 1)) - {e["seq"] for e in evs})
        (q["A6"].problems if ev_declared_full else q["A6"].missing).append(
            f"evidence entries {gaps[:6]}{'...' if len(gaps) > 6 else ''} are not in the pack, so the evidence chain cannot be walked")
    else:
        q["A6"].problems += _walk(evs, case_id, lambda e: e["content"], "captured_at")
    heads = {e["entry_hash"] for e in evs}
    gen_head = d.get("evidence_head_at_generation")
    ca = first.get("case_assembler", {})
    if gen_head and evs and gen_head not in heads and ev_contiguous:
        q["A6"].problems.append("the evidence state the draft was generated from is no longer in the chain")
    if gen_head and (ca.get("evidence_head") != gen_head or fb.get("input_hash") != gen_head):
        q["A6"].problems.append("draft header and chained trail disagree on the evidence it was generated from")
    if pack.get("pack_hash") != _ch({k: v for k, v in pack.items() if k != "pack_hash"}):
        q["A6"].problems.append("pack hash does not match the pack")
    if fiu_copy is not None and not declined:
        exp = [t for t in mine if t["action"] == "FILING_EXPORTED"]
        if not exp or exp[-1]["detail"].get("payload_hash") != _h(fiu_copy):
            q["A6"].problems.append("the report held by the FIU does not match the filing recorded in the trail")
        import html as _html
        if any(_html.escape(c["text"], quote=False) not in fiu_copy and c["text"] not in fiu_copy for c in active):
            q["A6"].problems.append("the narrative in the pack differs from the report held by the FIU")

    # ── A7 reproducibility ─────────────────────────────────────────────────
    pinned = {"verifier": {v.get("verifier") for c in active for v in [c.get("A2_verdict") or {}]} - {None},
              "catalogue": {h.get("catalogue_version") for h in hyps.values()} - {None}}
    if not declined and not pinned["verifier"]:
        q["A7"].missing.append("no pinned verifier version")
    if not pinned["catalogue"]:
        q["A7"].missing.append("no pinned typology catalogue version")
    if not (ev_contiguous and evs):
        q["A7"].missing.append("full evidence not in the pack, so facts cannot be recomputed")
    if not facts_complete:
        q["A7"].missing.append("full fact table not in the pack")
    if not declined and not (pack.get("outline") or {}).get("slots"):
        q["A7"].missing.append("the planner's allow-lists are not in the pack, so the verifier cannot be re-run")
    thr = (first.get("typology_analyst") or {}).get("abstain_confidence")
    if thr is None:
        q["A7"].missing.append("the confidence threshold used is not recorded")
    if not q["A7"].missing and replay is not None:
        r = replay(pack, thr)
        if not declined and r["versions"]["verifier"] not in pinned["verifier"]:
            q["A7"].missing.append(f"pinned verifier {pinned['verifier']} is not the released one {r['versions']['verifier']}")
        if r["versions"]["catalogue"] not in pinned["catalogue"]:
            q["A7"].missing.append("pinned typology catalogue is not the released one")
        if not q["A7"].missing:
            diff_f = [fid for fid, f in facts.items() if r["fact_hashes"].get(fid) != f["hash"]]
            diff_h = [hid for hid, h in hyps.items() if r["hyp_hashes"].get(hid) != h["hash"]]
            diff_v = [c["id"] for c in active if r["verdicts"].get(c["id"]) != c["A2_verdict"]["status"]]
            if diff_f:
                q["A7"].problems.append(f"{len(diff_f)} fact(s) do not reproduce from the evidence")
            if diff_h:
                q["A7"].problems.append(f"{len(diff_h)} hypothesis record(s) do not reproduce")
            if diff_v:
                q["A7"].problems.append(f"{len(diff_v)} verdict(s) do not reproduce")
            if declined and pack.get("abstain") and r.get("abstain") is False:
                q["A7"].problems.append("re-running the planner does not decline")
    elif replay is None and not q["A7"].missing:
        q["A7"].missing.append("no released code supplied for replay")

    # ── A8 typology ────────────────────────────────────────────────────────
    ty = runs.get("typology_analyst", [])
    if not hyps or not ty:
        q["A8"].missing.append("no typology record")
    else:
        if ty[0]["detail"].get("output_hash") != _ch([h["hash"] for h in pack["A8_hypotheses"]]):
            q["A8"].problems.append("hypotheses in the pack are not the ones recorded in the trail")
        for h in hyps.values():
            hyp_ok(h["id"], q["A8"])
            if thr is not None and h["supported"] != (h["confidence"] >= thr):
                q["A8"].problems.append(f"{h['id']}: 'supported' does not follow from its confidence and the threshold")
        evaluated = ty[0]["detail"].get("evaluated")
        if not evaluated:
            q["A8"].missing.append("counter-indicators that did not fire are not recorded, so the record cannot show they were checked")
        else:
            for h in hyps.values():
                rec = next((x for x in evaluated if x["typology"] == h["typology_id"]), None)
                fired_c = {i["id"] for i in h["counter"]}
                fired_s = {i["id"] for i in h["support"]}
                if not rec:
                    q["A8"].problems.append(f"{h['id']}: no evaluation record")
                    continue
                if {i["id"] for i in rec["counter"] if i["fired"]} != fired_c or {i["id"] for i in rec["support"] if i["fired"]} != fired_s:
                    q["A8"].problems.append(f"{h['id']}: evaluation record disagrees with the hypothesis")
        if thr is None:
            q["A8"].missing.append("threshold for 'supported' not recorded")

    # ── A9 data boundary ───────────────────────────────────────────────────
    if declined:
        if runs.get("drafter"):
            q["A9"].problems.append("a drafter ran on a declined case")
        else:
            q["A9"].na = True
    else:
        dr = first.get("drafter") or {}
        for k in ("id", "location", "in_boundary", "approval_ref", "pseudonymise"):
            if k not in endpoint:
                q["A9"].missing.append(f"endpoint record lacks {k}")
        if dr.get("in_boundary") != endpoint.get("in_boundary") or dr.get("pseudonymised") != endpoint.get("pseudonymise"):
            q["A9"].problems.append("draft header and drafter run disagree on the data boundary")
        if endpoint.get("in_boundary") is False:
            if not dr.get("pseudonymised") or not dr.get("pii_entities_masked"):
                q["A9"].problems.append("data sent outside the boundary without pseudonymisation")
            if endpoint.get("approval_ref") in (None, "", "n/a"):
                q["A9"].problems.append("endpoint outside the boundary has no approval reference")

    # ── A10 performance ────────────────────────────────────────────────────
    rel = pack.get("release")
    if not rel:
        q["A10"].missing.append("the pack does not name the system release, so its benchmark results cannot be found")
    else:
        comp = rel.get("components", {})
        if not declined and comp.get("verifier") not in pinned["verifier"]:
            q["A10"].problems.append("release verifier differs from the one that checked this report")
        if comp.get("typology_catalogue") not in pinned["catalogue"]:
            q["A10"].problems.append("release typology catalogue differs from the one used")
        if comp.get("template") != d.get("template"):
            q["A10"].problems.append("release template differs from the one used")
        b = rel.get("benchmark") or {}
        if release_root is None:
            q["A10"].missing.append("released benchmark results not supplied")
        else:
            p = release_root / b.get("path", "")
            if not b.get("path") or not p.is_file():
                q["A10"].missing.append("benchmark results file named by the release not found")
            elif hashlib.sha256(p.read_bytes()).hexdigest() != b.get("sha256"):
                q["A10"].problems.append("benchmark results differ from those published with the release")

    res = {k: {"status": v.status(), "problems": v.problems, "missing": v.missing} for k, v in q.items()}
    st = [r["status"] for r in res.values()]
    verdict = "FAIL" if "problem" in st else "INCOMPLETE" if "unanswerable" in st else "PASS"
    return {"case": case_id, "draft": did, "declined": declined, "questions": res, "verdict": verdict}
