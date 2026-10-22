"""Orchestrator: runs agents 0–7 for one draft and records every step.

Only this module writes drafts, sentences and trail entries during
generation. Agents receive data and return data; none of them holds a
database handle.

Every stage appends an AgentRun to the case's hash-chained trail with the
tool or model identity, the prompt hash (for model calls) and hashes of its
inputs and outputs (audit question A3).
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from typing import Any

from app import chain, db
from app.config import settings
from app.db import dumps
from app.ledger import append_trail, evidence_head, get_case, list_evidence
from app.pipeline import drafter as drafter_mod
from app.pipeline import gateway
from app.pipeline.facts import build_facts
from app.pipeline.planner import plan
from app.pipeline.typology import analyse
from app.templates import load_template
from app.verification import VERIFIER_VERSION, completeness, verify_sentences
from app.verification.claims import scan_evidence_for_injection

STAGE_DELAY = float(os.environ.get("SAR_STAGE_DELAY", "0.35"))


def _progress(draft_id: str, stage: str, pct: int) -> None:
    with db.session() as conn:
        conn.execute("UPDATE drafts SET stage=?, pct=?, updated_at=? WHERE id=?", (stage, pct, chain.now_iso(), draft_id))
    if STAGE_DELAY:
        time.sleep(STAGE_DELAY)


def _run(conn: Any, case_id: str, draft_id: str, agent: str, detail: dict[str, Any]) -> None:
    append_trail(conn, case_id, "agent_run", f"agent:{agent}", "AGENT_RUN", {"agent": agent, **detail}, draft_id)


def content_hash_of(sentences: list[dict[str, Any]], facts_hash: str) -> str:
    body = [{"id": s["id"], "section": s["section"], "text": s["text"], "cites": s["cites"]}
            for s in sentences if s["status"] == "active"]
    return chain.content_hash({"sentences": body, "facts": facts_hash})


def start(draft_id: str) -> threading.Thread:
    t = threading.Thread(target=run, args=(draft_id,), daemon=True, name=f"draft-{draft_id}")
    t.start()
    return t


def run(draft_id: str) -> None:
    try:
        _run_inner(draft_id)
    except Exception as e:  # surface every failure on the draft, never lose it silently
        with db.session() as conn:
            d = conn.execute("SELECT case_id FROM drafts WHERE id=?", (draft_id,)).fetchone()
            conn.execute("UPDATE drafts SET status='FAILED', stage='FAILED', error=?, updated_at=? WHERE id=?",
                         (f"{type(e).__name__}: {e}", chain.now_iso(), draft_id))
            if d:
                append_trail(conn, d["case_id"], "system", "orchestrator", "DRAFT_FAILED",
                             {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]}, draft_id)


def _run_inner(draft_id: str) -> None:
    with db.session() as conn:
        d = db.row(conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone())
        assert d is not None
        case = get_case(conn, d["case_id"])
        evidence = list_evidence(conn, d["case_id"])
        head = evidence_head(conn, d["case_id"])
        conn.execute("UPDATE drafts SET status='GENERATING', updated_at=? WHERE id=?", (chain.now_iso(), draft_id))
        _run(conn, case["id"], draft_id, "case_assembler", {"tool": "intake@1.0.0", "evidence_items": len(evidence),
                                                            "evidence_head": head})
    case_id = case["id"]
    template = load_template(case["jurisdiction"])
    provider = d["provider"]
    _progress(draft_id, "ASSEMBLE", 10)

    # 2. Fact Builder
    ft = build_facts(evidence)
    facts = ft.facts
    facts_hash = chain.content_hash([f["hash"] for f in facts])
    with db.session() as conn:
        _run(conn, case_id, draft_id, "fact_builder", {"tool": "facts@1.0.0", "input_hash": head,
                                                       "output_hash": facts_hash, "facts": len(facts)})
    _progress(draft_id, "FACTS", 25)

    # 3. Typology Analyst
    hyps = analyse(ft, settings.abstain_confidence)
    with db.session() as conn:
        _run(conn, case_id, draft_id, "typology_analyst", {
            "tool": "typology@1.0.0", "input_hash": facts_hash, "output_hash": chain.content_hash([h["hash"] for h in hyps]),
            "hypotheses": [{"id": h["id"], "typology": h["typology_id"], "confidence": h["confidence"], "supported": h["supported"]} for h in hyps]})
    _progress(draft_id, "TYPOLOGY", 40)

    # 4. Planner (may abstain)
    outline = plan(ft, hyps, template)
    injection = scan_evidence_for_injection(evidence)
    with db.session() as conn:
        conn.execute("UPDATE drafts SET facts_json=?, hypotheses_json=?, evidence_head=?, updated_at=? WHERE id=?",
                     (dumps(facts), dumps(hyps), head, chain.now_iso(), draft_id))
        _run(conn, case_id, draft_id, "planner", {"tool": "planner@1.0.0", "template": f"{template['id']}@{template['version']}",
                                                  "abstain": "abstain" in outline,
                                                  "slots": len(outline.get("slots", [])), "gaps": outline.get("gaps", [])})
    _progress(draft_id, "PLAN", 50)
    if "abstain" in outline:
        with db.session() as conn:
            stats = {"sentences": 0, "abstained": True, "injection_flags": injection}
            conn.execute("UPDATE drafts SET status='ABSTAINED', stage='DONE', pct=100, abstain_json=?, stats_json=?,"
                         " updated_at=? WHERE id=?", (dumps(outline["abstain"]), dumps(stats), chain.now_iso(), draft_id))
            append_trail(conn, case_id, "system", "orchestrator", "DRAFT_ABSTAINED",
                         {"decision": "INSUFFICIENT_GROUNDS", "best": outline["abstain"]["best_hypothesis"],
                          "evidence_head": head}, draft_id)
        return

    # 5. Drafter
    if provider == "offline":
        dr: Any = drafter_mod.OfflineDrafter(facts, hyps)
    else:
        ok, why = gateway.available(provider)
        if not ok:
            raise gateway.ModelError(f"Provider '{provider}' unavailable: {why}")
        dr = drafter_mod.LLMDrafter(provider, facts, hyps)
    endpoint = gateway.ENDPOINTS[provider]
    sents = dr.draft(outline)
    if d["inject_faults"]:
        sents = drafter_mod.inject_faults(sents, facts)
    with db.session() as conn:
        _run(conn, case_id, draft_id, "drafter", {
            "endpoint_id": endpoint.id, "model": endpoint.model, "location": endpoint.location,
            "in_boundary": endpoint.in_boundary, "pseudonymised": endpoint.pseudonymise,
            "pii_entities_masked": getattr(getattr(dr, "pii", None), "count", 0),
            "prompt_template": drafter_mod.PROMPT_VERSION, "prompt_hash": getattr(dr, "last_prompt_hash", "") or None,
            "input_hash": facts_hash, "output_hash": chain.content_hash(sents), "sentences": len(sents),
            "faults_injected": [s["fault"] for s in sents if s.get("fault")], **dr.run_meta()})
    _progress(draft_id, "DRAFT", 65)

    # 6. Verification + repair loop
    section_of = {s["slot"]: s["section"] for s in outline["slots"]}
    items = [{**s, "history": [{"event": "drafted", "text": s["text"], "cites": s["cites"]}]} for s in sents]
    verdicts = verify_sentences(items, facts, hyps, outline["slots"], template["style"])
    loop = 0
    while loop < settings.max_repair_loops:
        failing = [{**it, "problems": [c["message"] for c in v["checks"] if c["status"] == "fail"]}
                   for it, v in zip(items, verdicts) if v["status"] == "failed"]
        if not failing:
            break
        loop += 1
        fixed = {f["slot"]: f for f in dr.repair(outline, failing)}
        for it in items:
            if it.get("slot") in fixed and any(f["slot"] == it["slot"] and f["text"] == it["text"] for f in failing):
                nf = fixed.pop(it["slot"])
                it["history"].append({"event": f"repaired (loop {loop})", "text": nf["text"], "cites": nf["cites"],
                                      "problems": next(f["problems"] for f in failing if f["text"] == it["text"])})
                it["text"], it["cites"] = nf["text"], nf["cites"]
                it.pop("fault", None)
        verdicts = verify_sentences(items, facts, hyps, outline["slots"], template["style"])
        with db.session() as conn:
            _run(conn, case_id, draft_id, "verifier", {"verifier": VERIFIER_VERSION, "loop": loop,
                                                       "failed_before": len(failing),
                                                       "failed_after": sum(v["status"] == "failed" for v in verdicts)})
    _progress(draft_id, "VERIFY", 80)

    # 7. Assemble: failed sentences are removed, never shown as part of the narrative
    removed = []
    active: list[dict[str, Any]] = []
    for it, v in zip(items, verdicts):
        if v["status"] == "failed":
            removed.append({"text": it["text"], "cites": it["cites"], "slot": it.get("slot"),
                            "problems": [c for c in v["checks"] if c["status"] == "fail"], "history": it["history"]})
        else:
            active.append({**it, "verdict": v})
    stored: list[dict[str, Any]] = []
    with db.session() as conn:
        for idx, it in enumerate(active, 1):
            sid = f"{draft_id}-S{idx:02d}"
            origin = "repaired" if len(it["history"]) > 1 else "drafted"
            conn.execute("INSERT INTO sentences (id, draft_id, idx, section, text, cites_json, verdict_json, status, origin,"
                         " history_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (sid, draft_id, idx, section_of.get(it["slot"], "other"), it["text"], dumps(it["cites"]),
                          dumps({**it["verdict"], "slot": it["slot"]}), "active", origin, dumps(it["history"])))
            stored.append({"id": sid, "section": section_of.get(it["slot"], "other"), "text": it["text"],
                           "cites": it["cites"], "status": "active"})
        checklist = completeness(stored, facts, hyps, template)
        chash = content_hash_of(stored, facts_hash)
        stats = {"sentences": len(stored), "supported": sum(a["verdict"]["status"] == "supported" for a in active),
                 "flagged": sum(a["verdict"]["status"] == "flagged" for a in active), "removed": len(removed),
                 "repaired": sum(len(a["history"]) > 1 for a in active), "repair_loops": loop,
                 "citations": sum(len(a["cites"]) for a in active), "injection_flags": injection,
                 "gaps": outline["gaps"]}
        conn.execute("UPDATE drafts SET status='NEEDS_REVIEW', stage='DONE', pct=100, outline_json=?, removed_json=?,"
                     " checklist_json=?, stats_json=?, content_hash=?, updated_at=? WHERE id=?",
                     (dumps(outline), dumps(removed), dumps(checklist), dumps(stats), chash, chain.now_iso(), draft_id))
        append_trail(conn, case_id, "system", "orchestrator", "DRAFT_COMPLETED",
                     {"content_hash": chash, "evidence_head": head, "facts_hash": facts_hash,
                      **{k: v for k, v in stats.items() if k != "injection_flags"}}, draft_id)
