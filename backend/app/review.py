"""Human review workflow: decision → review → submit (maker) → approve (checker) → file.

Rules enforced here, all recorded in the hash-chained trail (A4, A5):
  - the SAR decision is a separate, human act with its own reason and a
    suspicion_formed_at timestamp that starts the deadline clock;
  - every claim is accepted, edited or rejected by a named analyst, with a
    reason code; edits are re-verified and refused if they introduce an
    unsupported number, name or date;
  - the approver must hold the MLRO role and must not have made or reviewed
    this draft (maker ≠ checker);
  - approval records the exact content hash the approver attested to.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app import chain, db
from app.db import dumps
from app.ledger import append_trail, get_case
from app.pipeline.orchestrator import content_hash_of
from app.templates import load_template
from app.verification import completeness, verify_sentences

REASON_CODES = {
    "ACCURATE": "Accurate as drafted",
    "EDIT_WORDING": "Wording improved, facts unchanged",
    "EDIT_FACTUAL": "Factual correction",
    "NOT_RELEVANT": "Not relevant to the grounds for suspicion",
    "UNSUPPORTED": "Not supported by the evidence",
    "DUPLICATE": "Duplicates another sentence",
    "OTHER": "Other (see note)",
}
EDITABLE = ("NEEDS_REVIEW", "IN_REVIEW", "RETURNED")


class ReviewError(Exception):
    def __init__(self, status: int, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.status, self.message, self.detail = status, message, detail


def _draft(conn: Any, draft_id: str) -> dict[str, Any]:
    d = db.row(conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone())
    if not d:
        raise ReviewError(404, "Draft not found")
    return d


def _sentences(conn: Any, draft_id: str) -> list[dict[str, Any]]:
    return db.rows(conn.execute("SELECT * FROM sentences WHERE draft_id=? ORDER BY idx", (draft_id,)).fetchall())


def _facts_hash(d: dict[str, Any]) -> str:
    return chain.content_hash([f["hash"] for f in d["facts"]])


def _refresh(conn: Any, d: dict[str, Any]) -> str:
    """Recompute checklist and content hash after any change to the sentences."""
    sents = _sentences(conn, d["id"])
    tmpl = load_template(d["jurisdiction"])
    checklist = completeness(sents, d["facts"], d["hypotheses"], tmpl)
    chash = content_hash_of(sents, _facts_hash(d))
    conn.execute("UPDATE drafts SET checklist_json=?, content_hash=?, updated_at=? WHERE id=?",
                 (dumps(checklist), chash, chain.now_iso(), d["id"]))
    return chash


# ── SAR decision ─────────────────────────────────────────────────────────────

def record_decision(case_id: str, user: dict[str, Any], sar_required: bool, reason: str,
                    suspicion_formed_at: str) -> dict[str, Any]:
    if len(reason.strip()) < 15:
        raise ReviewError(422, "Give a reason of at least 15 characters for the SAR decision")
    try:
        formed = datetime.fromisoformat(suspicion_formed_at)
    except ValueError as e:
        raise ReviewError(422, "suspicion_formed_at must be an ISO date-time") from e
    if formed.tzinfo is None:  # the UI sends local wall-clock time (PKT)
        formed = formed.replace(tzinfo=timezone(timedelta(hours=5)))
    formed = formed.astimezone(timezone.utc)
    if formed > datetime.now(timezone.utc):
        raise ReviewError(422, "Suspicion cannot be formed in the future")
    with db.session() as conn:
        case = get_case(conn, case_id)
        if not case:
            raise ReviewError(404, "Case not found")
        tmpl = load_template(case["jurisdiction"])
        deadline = (formed + timedelta(days=tmpl["deadline"]["days"])).isoformat()
        did = f"DEC-{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO decisions (id, case_id, sar_required, reason, suspicion_formed_at, deadline_at,"
                     " decided_by, decided_at) VALUES (?,?,?,?,?,?,?,?)",
                     (did, case_id, int(sar_required), reason.strip(), formed.isoformat(), deadline, user["id"], chain.now_iso()))
        conn.execute("UPDATE cases SET status=? WHERE id=?", ("SAR_REQUIRED" if sar_required else "NO_SAR", case_id))
        append_trail(conn, case_id, "decision", user["email"], "SAR_DECISION_RECORDED",
                     {"decision_id": did, "sar_required": sar_required, "reason": reason.strip(),
                      "suspicion_formed_at": formed.isoformat(), "deadline_at": deadline,
                      "deadline_rule": tmpl["deadline"]})
    return {"id": did, "deadline_at": deadline}


def latest_decision(conn: Any, case_id: str) -> dict[str, Any] | None:
    return db.row(conn.execute("SELECT * FROM decisions WHERE case_id=? ORDER BY decided_at DESC LIMIT 1", (case_id,)).fetchone())


# ── Claim review ─────────────────────────────────────────────────────────────

def claim_action(draft_id: str, sentence_id: str, user: dict[str, Any], action: str, reason_code: str,
                 note: str = "", text: str | None = None, cites: list[str] | None = None) -> dict[str, Any]:
    if action not in ("accept", "reject", "edit"):
        raise ReviewError(422, "action must be accept, reject or edit")
    if reason_code not in REASON_CODES:
        raise ReviewError(422, f"reason_code must be one of {', '.join(REASON_CODES)}")
    with db.session() as conn:
        d = _draft(conn, draft_id)
        if d["status"] not in EDITABLE:
            raise ReviewError(409, f"Draft is {d['status']}; claims can only be reviewed before submission")
        s = db.row(conn.execute("SELECT * FROM sentences WHERE id=? AND draft_id=?", (sentence_id, draft_id)).fetchone())
        if not s:
            raise ReviewError(404, "Sentence not found")
        before = {"text": s["text"], "cites": s["cites"], "status": s["status"], "review_status": s["review_status"]}
        history = s["history"]
        tmpl = load_template(d["jurisdiction"])
        if action == "edit":
            new_text = (text or "").strip()
            new_cites = cites if cites is not None else s["cites"]
            if not new_text:
                raise ReviewError(422, "Edited text is empty")
            slot = s["verdict"].get("slot")
            verdict = verify_sentences([{"slot": slot, "text": new_text, "cites": new_cites}], d["facts"],
                                       d["hypotheses"], d["outline"]["slots"], tmpl["style"])[0]
            if verdict["status"] == "failed":
                append_trail(conn, d["case_id"], "review", user["email"], "EDIT_REFUSED",
                             {"sentence_id": sentence_id, "attempted_text": new_text, "cites": new_cites,
                              "problems": [c["message"] for c in verdict["checks"] if c["status"] == "fail"]}, draft_id)
                raise ReviewError(422, "The edit fails verification and was not saved",
                                  [c for c in verdict["checks"] if c["status"] == "fail"])
            history.append({"event": "edited", "by": user["email"], "text": new_text, "cites": new_cites, "reason": reason_code})
            conn.execute("UPDATE sentences SET text=?, cites_json=?, verdict_json=?, review_status='edited', review_reason=?,"
                         " history_json=?, origin='human' WHERE id=?",
                         (new_text, dumps(new_cites), dumps({**verdict, "slot": slot}), reason_code, dumps(history), sentence_id))
        elif action == "accept":
            if s["verdict"]["status"] == "failed":
                raise ReviewError(409, "A sentence that fails verification cannot be accepted")
            conn.execute("UPDATE sentences SET review_status='accepted', review_reason=? WHERE id=?", (reason_code, sentence_id))
        else:
            history.append({"event": "rejected", "by": user["email"], "reason": reason_code, "note": note})
            conn.execute("UPDATE sentences SET status='rejected', review_status='rejected', review_reason=?, history_json=? WHERE id=?",
                         (reason_code, dumps(history), sentence_id))
        if d["status"] in ("NEEDS_REVIEW", "RETURNED"):
            conn.execute("UPDATE drafts SET status='IN_REVIEW' WHERE id=?", (draft_id,))
        chash = _refresh(conn, d)
        after = db.row(conn.execute("SELECT text, cites_json, status, review_status FROM sentences WHERE id=?", (sentence_id,)).fetchone())
        append_trail(conn, d["case_id"], "review", user["email"], f"CLAIM_{action.upper()}",
                     {"sentence_id": sentence_id, "reason_code": reason_code, "note": note, "before": before,
                      "after": after, "draft_content_hash": chash}, draft_id)
    return {"ok": True, "content_hash": chash}


def accept_all_supported(draft_id: str, user: dict[str, Any]) -> dict[str, Any]:
    with db.session() as conn:
        d = _draft(conn, draft_id)
        if d["status"] not in EDITABLE:
            raise ReviewError(409, f"Draft is {d['status']}")
        ids = [s["id"] for s in _sentences(conn, draft_id)
               if s["status"] == "active" and s["review_status"] == "pending" and s["verdict"]["status"] == "supported"]
        for sid in ids:
            conn.execute("UPDATE sentences SET review_status='accepted', review_reason='ACCURATE' WHERE id=?", (sid,))
        conn.execute("UPDATE drafts SET status='IN_REVIEW' WHERE id=?", (draft_id,))
        chash = _refresh(conn, d)
        append_trail(conn, d["case_id"], "review", user["email"], "CLAIMS_ACCEPTED_BULK",
                     {"sentence_ids": ids, "rule": "verifier status = supported", "draft_content_hash": chash}, draft_id)
    return {"accepted": len(ids)}


# ── Maker / checker ──────────────────────────────────────────────────────────

def submit(draft_id: str, user: dict[str, Any]) -> dict[str, Any]:
    with db.session() as conn:
        d = _draft(conn, draft_id)
        if d["status"] not in ("IN_REVIEW", "NEEDS_REVIEW", "RETURNED"):
            raise ReviewError(409, f"Draft is {d['status']} and cannot be submitted")
        dec = latest_decision(conn, d["case_id"])
        blockers = []
        if not dec or not dec["sar_required"]:
            blockers.append("Record a SAR decision (SAR required, with reason and suspicion date) before submitting.")
        sents = _sentences(conn, draft_id)
        pending = [s["id"] for s in sents if s["status"] == "active" and s["review_status"] == "pending"]
        if pending:
            blockers.append(f"{len(pending)} sentence(s) not yet reviewed.")
        if any(s["status"] == "active" and s["verdict"]["status"] == "failed" for s in sents):
            blockers.append("A sentence fails verification.")
        missing = [c["label"] for c in _refresh_checklist(conn, d) if c["status"] == "missing"]
        if missing:
            blockers.append("Checklist incomplete: " + "; ".join(missing))
        if blockers:
            raise ReviewError(409, "Draft cannot be submitted yet", blockers)
        chash = _refresh(conn, d)
        conn.execute("UPDATE drafts SET status='PENDING_APPROVAL', maker_id=?, submitted_at=? WHERE id=?",
                     (user["id"], chain.now_iso(), draft_id))
        append_trail(conn, d["case_id"], "review", user["email"], "SUBMITTED_FOR_APPROVAL",
                     {"maker": user["email"], "draft_content_hash": chash}, draft_id)
    return {"status": "PENDING_APPROVAL", "content_hash": chash}


def _refresh_checklist(conn: Any, d: dict[str, Any]) -> list[dict[str, Any]]:
    _refresh(conn, d)
    return db.row(conn.execute("SELECT checklist_json FROM drafts WHERE id=?", (d["id"],)).fetchone())["checklist"]


MAKER_ACTIONS = ("CLAIM_ACCEPT", "CLAIM_EDIT", "CLAIM_REJECT", "CLAIMS_ACCEPTED_BULK", "SUBMITTED_FOR_APPROVAL")


def reviewers(conn: Any, draft_id: str) -> set[str]:
    """Everyone who acted as a maker on this draft. Returning a draft is a checker action and does not count."""
    q = f"SELECT actor FROM trail WHERE draft_id=? AND action IN ({','.join('?' * len(MAKER_ACTIONS))})"
    return {r["actor"] for r in conn.execute(q, (draft_id, *MAKER_ACTIONS)).fetchall()}


def approve(draft_id: str, user: dict[str, Any], attestation: str, expected_hash: str) -> dict[str, Any]:
    with db.session() as conn:
        d = _draft(conn, draft_id)
        if d["status"] != "PENDING_APPROVAL":
            raise ReviewError(409, f"Draft is {d['status']}; only drafts pending approval can be approved")
        if user["id"] == d["maker_id"] or user["email"] in reviewers(conn, d["id"]):
            append_trail(conn, d["case_id"], "review", user["email"], "APPROVAL_REFUSED",
                         {"reason": "maker-checker: approver reviewed or submitted this draft"}, draft_id)
            raise ReviewError(403, "Maker–checker: you reviewed or submitted this draft, so you cannot approve it")
        if len(attestation.strip()) < 20:
            raise ReviewError(422, "Write an attestation statement (at least 20 characters)")
        if expected_hash != d["content_hash"]:
            raise ReviewError(409, "The draft changed since you opened it. Reload and review again.")
        now = chain.now_iso()
        conn.execute("UPDATE drafts SET status='APPROVED', checker_id=?, attestation=?, approved_at=? WHERE id=?",
                     (user["id"], attestation.strip(), now, draft_id))
        conn.execute("UPDATE cases SET status='SAR_APPROVED' WHERE id=?", (d["case_id"],))
        append_trail(conn, d["case_id"], "review", user["email"], "APPROVED_AND_ATTESTED",
                     {"checker": user["email"], "attestation": attestation.strip(), "draft_content_hash": d["content_hash"],
                      "evidence_head": d["evidence_head"]}, draft_id)
    return {"status": "APPROVED", "content_hash": d["content_hash"]}


def return_draft(draft_id: str, user: dict[str, Any], reason: str) -> dict[str, Any]:
    if len(reason.strip()) < 10:
        raise ReviewError(422, "Give a reason for returning the draft")
    with db.session() as conn:
        d = _draft(conn, draft_id)
        if d["status"] != "PENDING_APPROVAL":
            raise ReviewError(409, f"Draft is {d['status']}")
        conn.execute("UPDATE drafts SET status='RETURNED' WHERE id=?", (draft_id,))
        append_trail(conn, d["case_id"], "review", user["email"], "RETURNED_TO_MAKER", {"reason": reason.strip()}, draft_id)
    return {"status": "RETURNED"}


def record_filing(draft_id: str, user: dict[str, Any], channel: str, payload: str, validation: dict[str, Any]) -> dict[str, Any]:
    with db.session() as conn:
        d = _draft(conn, draft_id)
        if d["status"] not in ("APPROVED", "FILED"):
            raise ReviewError(409, "Only an approved draft can be exported for filing")
        fid = f"FIL-{uuid.uuid4().hex[:8]}"
        phash = chain.sha256(payload)
        conn.execute("INSERT INTO filings (id, draft_id, channel, payload, payload_hash, validation_json, created_by, created_at)"
                     " VALUES (?,?,?,?,?,?,?,?)", (fid, draft_id, channel, payload, phash, dumps(validation), user["id"], chain.now_iso()))
        append_trail(conn, d["case_id"], "filing", user["email"], "FILING_EXPORTED",
                     {"filing_id": fid, "channel": channel, "payload_hash": phash, "valid": validation["valid"],
                      "draft_content_hash": d["content_hash"]}, draft_id)
    return {"id": fid, "payload_hash": phash}


def mark_filed(draft_id: str, user: dict[str, Any], fiu_reference: str) -> dict[str, Any]:
    if len(fiu_reference.strip()) < 3:
        raise ReviewError(422, "Enter the FIU acknowledgment reference")
    with db.session() as conn:
        d = _draft(conn, draft_id)
        f = conn.execute("SELECT id FROM filings WHERE draft_id=? ORDER BY created_at DESC LIMIT 1", (draft_id,)).fetchone()
        if d["status"] != "APPROVED" or not f:
            raise ReviewError(409, "Export the approved report before recording the FIU submission")
        now = chain.now_iso()
        conn.execute("UPDATE filings SET fiu_reference=?, submitted_at=? WHERE id=?", (fiu_reference.strip(), now, f["id"]))
        conn.execute("UPDATE drafts SET status='FILED' WHERE id=?", (draft_id,))
        conn.execute("UPDATE cases SET status='SAR_FILED' WHERE id=?", (d["case_id"],))
        dec = latest_decision(conn, d["case_id"])
        on_time = bool(dec and datetime.fromisoformat(now) <= datetime.fromisoformat(dec["deadline_at"]))
        append_trail(conn, d["case_id"], "filing", user["email"], "FILED_WITH_FIU",
                     {"filing_id": f["id"], "fiu_reference": fiu_reference.strip(), "submitted_at": now,
                      "deadline_at": dec["deadline_at"] if dec else None, "on_time": on_time}, draft_id)
    return {"status": "FILED", "on_time": on_time}
