"""Auditability Report: the output of the "Verify" button.

Answers, from stored records alone:
  A6  has any evidence or trail record been altered?        (chain walks, anchors)
  A1  which facts and sentences rest on altered evidence?   (lineage impact)
  A7  do the facts and verdicts reproduce today?            (fact replay, verifier replay)
  A4  does the draft still match what was approved?         (content hash vs. attested hash)

The chain walk and verifier replay use the independent verification package.
Fact replay re-runs the deterministic Fact Builder over the current evidence,
which is how a changed deposit amount shows up as "F17 no longer reproduces".
"""
from __future__ import annotations

from typing import Any

from app import chain, db
from app.ledger import append_trail, get_case, list_evidence, list_trail
from app.pipeline.facts import build_facts
from app.pipeline.orchestrator import content_hash_of
from app.templates import load_template
from app.verification import verify_sentences
from app.verification.claims import scan_evidence_for_injection
from app.verification.integrity import (lineage_impact, verify_evidence_chain, verify_fact_hashes,
                                        verify_trail_chain)


def _raw_evidence(conn: Any, case_id: str) -> list[dict[str, Any]]:
    """Read evidence rows tolerantly: a tampered row may hold invalid JSON."""
    import json
    out = []
    for r in conn.execute("SELECT * FROM evidence WHERE case_id=? ORDER BY seq", (case_id,)).fetchall():
        e = dict(r)
        try:
            e["content"] = json.loads(e.pop("content_json"))
        except json.JSONDecodeError:
            e["content"] = {"_unparseable": e.pop("content_json", "")}
        out.append(e)
    return out


def verify_case(case_id: str, draft_id: str | None, actor: str, record: bool = True) -> dict[str, Any]:
    conn = db.connect()
    try:
        case = get_case(conn, case_id)
        if not case:
            raise KeyError(case_id)
        evidence = _raw_evidence(conn, case_id)
        trail = list_trail(conn, case_id)
        ev_chain = verify_evidence_chain(case_id, evidence)
        tr_chain = verify_trail_chain(case_id, [dict(t) for t in trail])
        if draft_id:
            d = db.row(conn.execute("SELECT * FROM drafts WHERE id=? AND case_id=?", (draft_id, case_id)).fetchone())
        else:
            d = db.row(conn.execute("SELECT * FROM drafts WHERE case_id=? AND status NOT IN ('FAILED','GENERATING','REQUESTED')"
                                    " ORDER BY version DESC LIMIT 1", (case_id,)).fetchone())
        sentences = db.rows(conn.execute("SELECT * FROM sentences WHERE draft_id=? ORDER BY idx", (d["id"],)).fetchall()) if d else []
    finally:
        conn.close()

    broken = {p["id"] for p in ev_chain["problems"] if p["kind"] != "gap"}
    report: dict[str, Any] = {
        "case_id": case_id, "checked_at": chain.now_iso(), "checked_by": actor,
        "evidence_chain": ev_chain, "trail_chain": tr_chain,
        "chain_integrity": "intact" if ev_chain["intact"] else f"broken_at {ev_chain['first_break']}",
        "trail_integrity": "intact" if tr_chain["intact"] else f"broken_at seq {tr_chain['problems'][0]['seq']}",
        "injection_flags": scan_evidence_for_injection([e for e in evidence if isinstance(e["content"], dict)]),
        "draft": None,
    }

    if d and d.get("facts"):
        facts, hyps = d["facts"], d["hypotheses"]
        active = [s for s in sentences if s["status"] == "active"]
        impact = lineage_impact(broken, facts, hyps, sentences)

        # Anchor: the evidence head recorded when the draft was generated must still be in the chain.
        anchored = d["evidence_head"]
        anchor_ok = any(e["entry_hash"] == anchored for e in evidence)

        # Fact replay over current evidence.
        changed = []
        try:
            now_facts = {f["id"]: f for f in build_facts([e for e in evidence]).facts}
            for f in facts:
                g = now_facts.get(f["id"])
                if not g or g["hash"] != f["hash"]:
                    changed.append({"id": f["id"], "label": f["label"], "stored": f["display"],
                                    "recomputed": g["display"] if g else "(no longer computable)"})
        except Exception as e:  # tampered content can break the calculation itself
            changed.append({"id": "*", "label": "Fact Builder", "stored": "", "recomputed": f"replay failed: {e}"})

        # Verifier replay on the stored sentences.
        tmpl = load_template(d["jurisdiction"])
        replay = []
        if d.get("outline"):
            items = [{"slot": s["verdict"].get("slot"), "text": s["text"], "cites": s["cites"]} for s in active]
            for s, v in zip(active, verify_sentences(items, facts, hyps, d["outline"]["slots"], tmpl["style"])):
                if v["status"] != s["verdict"]["status"]:
                    replay.append({"sentence_id": s["id"], "stored": s["verdict"]["status"], "replayed": v["status"]})

        recomputed_hash = content_hash_of(sentences, chain.content_hash([f["hash"] for f in facts]))
        has_narrative = d["content_hash"] is not None or bool(active)  # a declined draft has no narrative to hash
        approved_entry = next((t for t in reversed(trail) if t["draft_id"] == d["id"] and t["action"] == "APPROVED_AND_ATTESTED"), None)
        attested = approved_entry["detail"]["draft_content_hash"] if approved_entry else None

        fact_ids = {f["id"] for f in facts} | {h["id"] for h in hyps}
        cites_total = sum(len(s["cites"]) for s in active)
        unresolved = sum(1 for s in active for c in s["cites"] if c not in fact_ids)
        tainted = set(impact["sentences"])
        ok_sentences = [s for s in active if s["cites"] and all(c in fact_ids for c in s["cites"])
                        and s["id"] not in tainted and s["verdict"]["status"] != "failed"]
        report["draft"] = {
            "draft_id": d["id"], "version": d["version"], "status": d["status"],
            "sentences_total": len(active), "sentences_fully_cited": sum(1 for s in active if s["cites"]),
            "citations_resolved": cites_total - unresolved, "citations_unresolved": unresolved,
            "auditability_score": round(len(ok_sentences) / len(active), 3) if active else None,
            "lineage_impact": impact, "facts_not_reproducible": changed,
            "fact_hash_mismatches": verify_fact_hashes(facts), "verdict_replay_differences": replay,
            "anchor": {"evidence_head_at_draft": anchored, "still_in_chain": anchor_ok},
            "content_hash": {"stored": d["content_hash"], "recomputed": recomputed_hash,
                             "attested": attested,
                             "matches_stored": (recomputed_hash == d["content_hash"]) if has_narrative else None,
                             "matches_attested": (attested == recomputed_hash) if attested else None},
        }

    dr = report["draft"]
    problems = []
    if not ev_chain["intact"]:
        problems.append(f"Evidence chain broken at {ev_chain['first_break']}")
    if not tr_chain["intact"]:
        problems.append("Audit trail chain broken")
    if dr:
        if not dr["anchor"]["still_in_chain"]:
            problems.append("Evidence chain was rewritten after this draft was generated")
        if dr["facts_not_reproducible"]:
            problems.append(f"{len(dr['facts_not_reproducible'])} fact(s) no longer reproduce from the evidence")
        if dr["content_hash"]["matches_stored"] is False:
            problems.append("Draft sentences changed outside the review workflow")
        if dr["content_hash"]["matches_attested"] is False:
            problems.append("Draft no longer matches what the MLRO attested")
    report["verdict"] = "PASS" if not problems else "FAIL"
    report["problems"] = problems

    if record:
        with db.session() as conn:
            append_trail(conn, case_id, "audit", actor, "INTEGRITY_VERIFIED",
                         {"verdict": report["verdict"], "problems": problems,
                          "evidence_head": ev_chain["head"], "draft_id": dr["draft_id"] if dr else None},
                         dr["draft_id"] if dr else None)
    return report
