"""HTTP API for Lumous SAR Studio."""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from app import audit, auth, chain, db, review
from app.adapters import audit_pack, goaml
from app.integrations import tazama
from app.intake import ingest_case
from app.config import settings
from app.db import dumps
from app.ledger import append_trail, get_case, list_evidence, list_trail
from app.pipeline import gateway, orchestrator
from app.templates import load_template

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    yield


app = FastAPI(title="Lumous SAR Studio", version="0.1.0", lifespan=lifespan,
              description="Auditable, agent-drafted and human-attested SAR/STR drafting for Tazama deployments.")

ANY_ROLE = auth.require("analyst", "mlro", "auditor")
MAKER = auth.require("analyst")
CHECKER = auth.require("mlro")
STAFF = auth.require("analyst", "mlro")


@app.exception_handler(review.ReviewError)
def _review_error(_: Request, e: review.ReviewError) -> JSONResponse:
    return JSONResponse(status_code=e.status, content={"detail": e.message, "problems": e.detail})


# ── Auth & system ────────────────────────────────────────────────────────────

class LoginIn(BaseModel):
    email: str
    password: str


@app.post("/api/auth/login")
def login(body: LoginIn) -> dict[str, Any]:
    res = auth.login(body.email, body.password)
    if not res:
        raise HTTPException(401, "Wrong email or password")
    return {"token": res[0], "user": res[1]}


@app.get("/api/auth/me")
def me(user: dict = Depends(ANY_ROLE)) -> dict[str, Any]:
    return user


@app.get("/api/system")
def system(user: dict = Depends(ANY_ROLE)) -> dict[str, Any]:
    eps = []
    for key, ep in gateway.ENDPOINTS.items():
        ok, why = gateway.available(key)
        eps.append({**ep.public(), "key": key, "available": ok, "status": why})
    tmpl = load_template("GOAML:PK")
    return {"endpoints": eps, "default_provider": settings.default_provider,
            "template": {"id": tmpl["id"], "version": tmpl["version"], "name": tmpl["name"], "deadline": tmpl["deadline"],
                         "reference": tmpl["reference"], "required_elements": tmpl["required_elements"]},
            "reason_codes": review.REASON_CODES, "abstain_confidence": settings.abstain_confidence,
            "max_repair_loops": settings.max_repair_loops}


def _users(conn: Any) -> dict[str, str]:
    return {r["id"]: f"{r['name']} ({r['role']})" for r in conn.execute("SELECT id, name, role FROM users").fetchall()}


# ── Cases ────────────────────────────────────────────────────────────────────

@app.get("/api/cases")
def cases(user: dict = Depends(ANY_ROLE)) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        out = []
        for c in db.rows(conn.execute("SELECT * FROM cases ORDER BY detected_at DESC").fetchall()):
            d = db.row(conn.execute("SELECT id, status, version, stats_json FROM drafts WHERE case_id=? ORDER BY version DESC LIMIT 1",
                                    (c["id"],)).fetchone())
            dec = review.latest_decision(conn, c["id"])
            n_ev = conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id=?", (c["id"],)).fetchone()[0]
            link = db.row(conn.execute("SELECT * FROM external_links WHERE case_id=? AND system='tazama-cms'",
                                       (c["id"],)).fetchone())
            out.append({**c, "latest_draft": d, "decision": dec, "evidence_count": n_ev, "tazama_link": link})
        return out
    finally:
        conn.close()


@app.get("/api/cases/{case_id}")
def case_detail(case_id: str, user: dict = Depends(ANY_ROLE)) -> dict[str, Any]:
    conn = db.connect()
    try:
        c = get_case(conn, case_id)
        if not c:
            raise HTTPException(404, "Case not found")
        drafts = db.rows(conn.execute("SELECT id, version, status, provider, inject_faults, stats_json, content_hash, created_at,"
                                      " requested_by FROM drafts WHERE case_id=? ORDER BY version DESC", (case_id,)).fetchall())
        users = _users(conn)
        for d in drafts:
            d["requested_by"] = users.get(d["requested_by"], d["requested_by"])
        dec = review.latest_decision(conn, case_id)
        if dec:
            dec["decided_by"] = users.get(dec["decided_by"], dec["decided_by"])
        links = db.rows(conn.execute("SELECT * FROM external_links WHERE case_id=?", (case_id,)).fetchall())
        return {**c, "evidence": list_evidence(conn, case_id), "drafts": drafts, "decision": dec, "links": links}
    finally:
        conn.close()


class DecisionIn(BaseModel):
    sar_required: bool
    reason: str
    suspicion_formed_at: str


@app.post("/api/cases/{case_id}/decision")
def decision(case_id: str, body: DecisionIn, user: dict = Depends(STAFF)) -> dict[str, Any]:
    return review.record_decision(case_id, user, body.sar_required, body.reason, body.suspicion_formed_at)


class DraftIn(BaseModel):
    provider: str = Field(default_factory=lambda: settings.default_provider)
    inject_faults: bool = False


@app.post("/api/cases/{case_id}/drafts", status_code=202)
def request_draft(case_id: str, body: DraftIn, user: dict = Depends(MAKER)) -> dict[str, Any]:
    if body.provider not in gateway.ENDPOINTS:
        raise HTTPException(422, f"Unknown provider {body.provider}")
    ok, why = gateway.available(body.provider)
    if not ok:
        raise HTTPException(409, f"Model endpoint '{body.provider}' is not available: {why}")
    with db.session() as conn:
        c = get_case(conn, case_id)
        if not c:
            raise HTTPException(404, "Case not found")
        busy = conn.execute("SELECT id FROM drafts WHERE case_id=? AND status IN ('REQUESTED','GENERATING')", (case_id,)).fetchone()
        if busy:
            raise HTTPException(409, "A draft is already being generated for this case")
        version = (conn.execute("SELECT MAX(version) FROM drafts WHERE case_id=?", (case_id,)).fetchone()[0] or 0) + 1
        tmpl = load_template(c["jurisdiction"])
        did = f"D{case_id.split('-')[-1]}-{version}-{uuid.uuid4().hex[:4]}"
        now = chain.now_iso()
        ep = gateway.ENDPOINTS[body.provider]
        conn.execute("INSERT INTO drafts (id, case_id, version, status, jurisdiction, template_version, provider, endpoint_json,"
                     " inject_faults, requested_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (did, case_id, version, "REQUESTED", c["jurisdiction"], f"{tmpl['id']}@{tmpl['version']}", body.provider,
                      dumps(ep.public()), int(body.inject_faults), user["id"], now, now))
        append_trail(conn, case_id, "system", user["email"], "DRAFT_REQUESTED",
                     {"provider": body.provider, "endpoint": ep.public(), "inject_faults": body.inject_faults, "version": version}, did)
    orchestrator.start(did)
    return {"id": did, "version": version}


@app.get("/api/cases/{case_id}/trail")
def trail(case_id: str, user: dict = Depends(ANY_ROLE)) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        return list_trail(conn, case_id)
    finally:
        conn.close()


@app.get("/api/cases/{case_id}/verify")
def verify(case_id: str, draft_id: str | None = None, user: dict = Depends(ANY_ROLE)) -> dict[str, Any]:
    try:
        return audit.verify_case(case_id, draft_id, user["email"])
    except KeyError:
        raise HTTPException(404, "Case not found")


# ── Drafts ───────────────────────────────────────────────────────────────────

def _load_draft(conn: Any, draft_id: str) -> dict[str, Any]:
    d = db.row(conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone())
    if not d:
        raise HTTPException(404, "Draft not found")
    return d


@app.get("/api/drafts/{draft_id}/progress")
def progress(draft_id: str, user: dict = Depends(ANY_ROLE)) -> dict[str, Any]:
    conn = db.connect()
    try:
        r = conn.execute("SELECT status, stage, pct, error FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if not r:
            raise HTTPException(404, "Draft not found")
        return dict(r)
    finally:
        conn.close()


@app.get("/api/drafts/{draft_id}")
def draft_detail(draft_id: str, user: dict = Depends(ANY_ROLE)) -> dict[str, Any]:
    conn = db.connect()
    try:
        d = _load_draft(conn, draft_id)
        case = get_case(conn, d["case_id"])
        sentences = db.rows(conn.execute("SELECT * FROM sentences WHERE draft_id=? ORDER BY idx", (draft_id,)).fetchall())
        evidence = {e["id"]: e for e in list_evidence(conn, d["case_id"])}
        lineage = {x for f in (d.get("facts") or []) for x in f["derived_from"]}
        users = _users(conn)
        dec = review.latest_decision(conn, d["case_id"])
        makers = review.reviewers(conn, draft_id)
        filing = db.row(conn.execute("SELECT id, channel, payload_hash, validation_json, created_at, fiu_reference, submitted_at"
                                     " FROM filings WHERE draft_id=? ORDER BY created_at DESC LIMIT 1", (draft_id,)).fetchone())
        tz_link = db.row(conn.execute("SELECT * FROM external_links WHERE case_id=? AND system='tazama-cms'",
                                      (d["case_id"],)).fetchone())
        tz_events = [r["action"] for r in conn.execute(
            "SELECT action FROM trail WHERE draft_id=? AND action LIKE 'TAZAMA_CMS_%'", (draft_id,)).fetchall()]
    finally:
        conn.close()
    tmpl = load_template(d["jurisdiction"])
    perms = {
        "review": user["role"] == "analyst" and d["status"] in review.EDITABLE,
        "submit": user["role"] == "analyst" and d["status"] in review.EDITABLE,
        "approve": user["role"] == "mlro" and d["status"] == "PENDING_APPROVAL",
        "approve_blocked_reason": ("You reviewed or submitted this draft (maker–checker)"
                                   if user["id"] == d["maker_id"] or user["email"] in makers else None),
        "export": user["role"] in ("analyst", "mlro") and d["status"] in ("APPROVED", "FILED"),
    }
    for k in ("maker_id", "checker_id", "requested_by"):
        d[k.replace("_id", "")] = users.get(d.get(k) or "", None)
    return {"draft": d, "case": case, "sentences": sentences, "decision": dec, "filing": filing,
            "evidence": {k: v for k, v in evidence.items() if k in lineage},
            "sections": tmpl["sections"], "reason_codes": review.REASON_CODES, "permissions": perms,
            "tazama": {"link": tz_link, "pushed": "TAZAMA_CMS_EVIDENCE_PUSHED" in tz_events,
                       "completed": "TAZAMA_CMS_TASK_COMPLETED" in tz_events} if tz_link else None}


class ClaimIn(BaseModel):
    action: str
    reason_code: str = "ACCURATE"
    note: str = ""
    text: str | None = None
    cites: list[str] | None = None


@app.patch("/api/drafts/{draft_id}/sentences/{sentence_id}")
def claim(draft_id: str, sentence_id: str, body: ClaimIn, user: dict = Depends(MAKER)) -> dict[str, Any]:
    return review.claim_action(draft_id, sentence_id, user, body.action, body.reason_code, body.note, body.text, body.cites)


@app.post("/api/drafts/{draft_id}/accept-supported")
def accept_supported(draft_id: str, user: dict = Depends(MAKER)) -> dict[str, Any]:
    return review.accept_all_supported(draft_id, user)


@app.post("/api/drafts/{draft_id}/submit")
def submit(draft_id: str, user: dict = Depends(MAKER)) -> dict[str, Any]:
    return review.submit(draft_id, user)


class ApproveIn(BaseModel):
    attestation: str
    content_hash: str


@app.post("/api/drafts/{draft_id}/approve")
def approve(draft_id: str, body: ApproveIn, user: dict = Depends(CHECKER)) -> dict[str, Any]:
    return review.approve(draft_id, user, body.attestation, body.content_hash)


class ReasonIn(BaseModel):
    reason: str


@app.post("/api/drafts/{draft_id}/return")
def return_draft(draft_id: str, body: ReasonIn, user: dict = Depends(CHECKER)) -> dict[str, Any]:
    return review.return_draft(draft_id, user, body.reason)


def _export_inputs(draft_id: str) -> tuple[dict, dict, list, list, dict]:
    conn = db.connect()
    try:
        d = _load_draft(conn, draft_id)
        case = get_case(conn, d["case_id"])
        sentences = db.rows(conn.execute("SELECT * FROM sentences WHERE draft_id=? ORDER BY idx", (draft_id,)).fetchall())
        evidence = list_evidence(conn, d["case_id"])
        mlro = db.row(conn.execute("SELECT * FROM users WHERE id=?", (d["checker_id"] or "",)).fetchone()) or \
            {"name": "Unassigned MLRO", "email": ""}
        return d, case, sentences, evidence, mlro
    finally:
        conn.close()


@app.post("/api/drafts/{draft_id}/export/goaml")
def export_goaml(draft_id: str, user: dict = Depends(STAFF)) -> dict[str, Any]:
    d, case, sentences, evidence, mlro = _export_inputs(draft_id)
    if d["status"] not in ("APPROVED", "FILED"):
        raise HTTPException(409, "Only an approved draft can be exported for filing")
    tmpl = load_template(d["jurisdiction"])
    xml, txns = goaml.build(case, d, sentences, evidence, tmpl, mlro)
    validation = goaml.validate(xml, d, tmpl)
    filing = review.record_filing(draft_id, user, "goAML", xml, validation)
    return {"filing": filing, "validation": validation, "transactions": len(txns), "xml": xml}


@app.get("/api/drafts/{draft_id}/export/goaml.xml")
def goaml_download(draft_id: str, user: dict = Depends(STAFF)) -> Response:
    conn = db.connect()
    try:
        f = conn.execute("SELECT payload FROM filings WHERE draft_id=? ORDER BY created_at DESC LIMIT 1", (draft_id,)).fetchone()
    finally:
        conn.close()
    if not f:
        raise HTTPException(404, "No export yet")
    return Response(f["payload"], media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="{draft_id}-goaml-str.xml"'})


class FiledIn(BaseModel):
    fiu_reference: str


@app.post("/api/drafts/{draft_id}/filed")
def filed(draft_id: str, body: FiledIn, user: dict = Depends(STAFF)) -> dict[str, Any]:
    return review.mark_filed(draft_id, user, body.fiu_reference)


def _audit_pack(draft_id: str, user: dict, fmt: str) -> dict[str, Any]:
    d, case, sentences, evidence, _ = _export_inputs(draft_id)
    integrity = audit.verify_case(d["case_id"], draft_id, user["email"])
    conn = db.connect()
    try:
        trail_rows = list_trail(conn, d["case_id"])
        dec = review.latest_decision(conn, d["case_id"])
        filings = db.rows(conn.execute("SELECT * FROM filings WHERE draft_id=?", (draft_id,)).fetchall())
        users = _users(conn)
    finally:
        conn.close()
    pack = audit_pack.build_json(case, d, sentences, evidence, trail_rows, dec, filings, integrity, users)
    with db.session() as conn:
        append_trail(conn, d["case_id"], "audit", user["email"], "AUDIT_PACK_EXPORTED",
                     {"format": fmt, "pack_hash": pack["pack_hash"]}, draft_id)
    return pack


@app.get("/api/drafts/{draft_id}/audit-pack")
def audit_pack_download(draft_id: str, format: str = "html", user: dict = Depends(ANY_ROLE)) -> Response:
    pack = _audit_pack(draft_id, user, format)
    if format == "json":
        return JSONResponse(pack, headers={"Content-Disposition": f'attachment; filename="{draft_id}-audit-pack.json"'})
    return HTMLResponse(audit_pack.build_html(pack),
                        headers={"Content-Disposition": f'attachment; filename="{draft_id}-audit-pack.html"'})


# ── Tazama CMS integration ───────────────────────────────────────────────────

class TazamaLinkIn(BaseModel):
    end_to_end_id: str
    cms_case_id: str
    cms_task_id: str | None = None
    cms_url: str


@app.post("/api/integrations/tazama/link")
def tazama_link(body: TazamaLinkIn, user: dict = Depends(STAFF)) -> dict[str, Any]:
    """Bind a SAR Studio case to the Tazama CMS case whose alert it came from.

    The match is on the alert's triggering transaction (OrgnlEndToEndId), which
    the Case Assembler stored as hash-chained evidence. The link is itself a
    trail event, so an examiner can see when and by whom the two were joined.
    """
    with db.session() as conn:
        row = conn.execute(
            "SELECT case_id FROM evidence WHERE type='tazama_alert'"
            " AND json_extract(content_json, '$.trigger.OrgnlEndToEndId')=?", (body.end_to_end_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"No case with a Tazama alert for transaction {body.end_to_end_id}")
        case_id = row["case_id"]
        prev = db.row(conn.execute("SELECT * FROM external_links WHERE case_id=? AND system='tazama-cms'",
                                   (case_id,)).fetchone())
        if prev and (prev["ext_case_id"], prev["ext_task_id"]) == (body.cms_case_id, body.cms_task_id):
            return {"case_id": case_id, "link": prev, "created": False}
        now = chain.now_iso()
        conn.execute("INSERT OR REPLACE INTO external_links VALUES (?,?,?,?,?,?,?)",
                     (case_id, "tazama-cms", body.cms_case_id, body.cms_task_id, body.cms_url, user["email"], now))
        append_trail(conn, case_id, "integration", user["email"], "TAZAMA_CMS_LINKED",
                     {"end_to_end_id": body.end_to_end_id, "cms_case_id": body.cms_case_id,
                      "cms_task_id": body.cms_task_id, "cms_url": body.cms_url})
        link = db.row(conn.execute("SELECT * FROM external_links WHERE case_id=? AND system='tazama-cms'",
                                   (case_id,)).fetchone())
        return {"case_id": case_id, "link": link, "created": True}


class TazamaPushedIn(BaseModel):
    draft_id: str
    cms_evidence_ids: list[str]


@app.post("/api/integrations/tazama/pushed")
def tazama_pushed(body: TazamaPushedIn, user: dict = Depends(STAFF)) -> dict[str, Any]:
    """Record that the attested SAR and audit pack were delivered to the CMS."""
    with db.session() as conn:
        d = _load_draft(conn, body.draft_id)
        append_trail(conn, d["case_id"], "integration", user["email"], "TAZAMA_CMS_EVIDENCE_PUSHED",
                     {"cms_evidence_ids": body.cms_evidence_ids, "content_hash": d["content_hash"]}, body.draft_id)
        return {"ok": True}


# ── Live Tazama connector (real CMS + Tazama message store) ──────────────────

def _tz_guard() -> None:
    if not tazama.enabled():
        raise HTTPException(503, "The Tazama connector is not configured (SAR_TAZAMA_CMS_URL, SAR_TAZAMA_CMS_USER, "
                                 "SAR_TAZAMA_PG_DSN).")


def _tz(fn, *a):
    try:
        return fn(*a)
    except tazama.TazamaError as e:
        raise HTTPException(502, str(e)) from None


@app.get("/api/integrations/tazama/status")
def tazama_status(user: dict = Depends(ANY_ROLE)) -> dict[str, Any]:
    return {"enabled": tazama.enabled(), "cms_url": settings.tazama_cms_ui_url or settings.tazama_cms_url}


@app.get("/api/integrations/tazama/inbox")
def tazama_inbox(user: dict = Depends(ANY_ROLE)) -> list[dict[str, Any]]:
    """Open SAR/STR Filing tasks waiting in the Tazama CMS."""
    _tz_guard()
    cms = _tz(tazama.Cms)
    items = _tz(cms.sar_tasks)
    conn = db.connect()
    try:
        imported = {r["ext_case_id"]: r["case_id"] for r in conn.execute(
            "SELECT ext_case_id, case_id FROM external_links WHERE system='tazama-cms'").fetchall()}
    finally:
        conn.close()
    out = []
    for it in items:
        c, t = it["case"], it["task"]
        tx = ((c.get("alert") or {}).get("transaction") or {}).get("FIToFIPmtSts", {}).get("TxInfAndSts", {})
        out.append({"cms_case_id": c["case_id"], "task_id": t["task_id"], "task_status": t["status"],
                    "case_type": c.get("case_type"), "priority": c.get("priority"), "updated_at": c.get("updated_at"),
                    "alert_id": (c.get("alert") or {}).get("alert_id"), "end_to_end_id": tx.get("OrgnlEndToEndId"),
                    "imported_as": imported.get(str(c["case_id"]))})
    return out


class TazamaImportIn(BaseModel):
    cms_case_id: int


@app.post("/api/integrations/tazama/import")
def tazama_import(body: TazamaImportIn, user: dict = Depends(MAKER)) -> dict[str, Any]:
    """Build (or refresh) a SAR Studio case from a confirmed Tazama CMS case."""
    _tz_guard()
    cms = _tz(tazama.Cms)
    bundle = _tz(tazama.build_bundle, cms, body.cms_case_id)
    case_id, info = bundle["case"]["id"], bundle["cms"]
    with db.session() as conn:
        exists = get_case(conn, case_id) is not None
        before = conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id=?", (case_id,)).fetchone()[0]
        ingest_case(conn, bundle, settings.tenant_id, actor=user["email"], refresh=exists)
        added = conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id=?", (case_id,)).fetchone()[0] - before
        url = f"{(settings.tazama_cms_ui_url or settings.tazama_cms_url).rstrip('/')}/cases/{info['case_id']}"
        if not exists:
            conn.execute("INSERT OR REPLACE INTO external_links VALUES (?,?,?,?,?,?,?)",
                         (case_id, "tazama-cms", str(info["case_id"]), str(info["task_id"] or ""), url,
                          user["email"], chain.now_iso()))
            append_trail(conn, case_id, "integration", user["email"], "TAZAMA_CMS_LINKED",
                         {"cms_case_id": info["case_id"], "cms_task_id": info["task_id"], "alert_id": info["alert_id"],
                          "end_to_end_id": info["e2e"], "tenant": info["tenant"], "cms_url": url})
    if info["task_id"]:
        _tz(cms.comment, info["case_id"], info["task_id"],
            (f"Imported into SAR Studio as {case_id}: {len(bundle['transactions'])} payments from Tazama, "
             f"KYC {'attached' if bundle['provenance']['kyc'][0].startswith('tazama-cms') else 'not attached'}, "
             f"{len(bundle['notes'])} investigator notes.") if not exists else
            f"SAR Studio case {case_id} refreshed from the CMS: {added} new evidence items appended.")
    return {"case_id": case_id, "created": not exists, "evidence_added": added,
            "transactions": len(bundle["transactions"]), "kyc_attached": bundle["provenance"]["kyc"][0].startswith("tazama-cms"),
            "screening_attached": bool(bundle["screening"]), "notes": len(bundle["notes"])}


@app.post("/api/integrations/tazama/push/{draft_id}")
def tazama_push(draft_id: str, user: dict = Depends(STAFF)) -> dict[str, Any]:
    """Deliver the attested STR to the CMS SAR/STR Filing task; complete it once filed."""
    _tz_guard()
    conn = db.connect()
    try:
        d = _load_draft(conn, draft_id)
        link = db.row(conn.execute("SELECT * FROM external_links WHERE case_id=? AND system='tazama-cms'",
                                   (d["case_id"],)).fetchone())
        filing = db.row(conn.execute("SELECT * FROM filings WHERE draft_id=? ORDER BY created_at DESC LIMIT 1",
                                     (draft_id,)).fetchone())
        pushed = conn.execute("SELECT COUNT(*) FROM trail WHERE draft_id=? AND action='TAZAMA_CMS_EVIDENCE_PUSHED'",
                              (draft_id,)).fetchone()[0]
        completed = conn.execute("SELECT COUNT(*) FROM trail WHERE draft_id=? AND action='TAZAMA_CMS_TASK_COMPLETED'",
                                 (draft_id,)).fetchone()[0]
        case = get_case(conn, d["case_id"])
    finally:
        conn.close()
    if not link or not link["ext_task_id"]:
        raise HTTPException(409, "This case is not linked to a Tazama CMS SAR/STR task")
    if d["status"] not in ("APPROVED", "FILED"):
        raise HTTPException(409, "Only an MLRO-approved draft can be sent to the CMS")
    cms = _tz(tazama.Cms)
    task_id, cms_case = int(link["ext_task_id"]), int(link["ext_case_id"])
    out: dict[str, Any] = {"cms_case_id": cms_case, "cms_task_id": task_id}
    if not pushed:
        if not filing:
            export_goaml(draft_id, user)
            filing = db.row(db.connect().execute("SELECT * FROM filings WHERE draft_id=? ORDER BY created_at DESC LIMIT 1",
                                                 (draft_id,)).fetchone())
        pack = _audit_pack(draft_id, user, "html")
        desc = (f"SAR/STR Filing - Regulatory Filing\n\nRemarks: STR drafted and verified in SAR Studio ({draft_id}), "
                f"approved by the MLRO. Attested content hash {d['content_hash']}; audit pack hash {pack['pack_hash']}. "
                f"goAML XML attached as .txt (the CMS does not accept .xml).")
        ids = _tz(cms.upload, task_id, [
            (f"STR-{case['ref']}-audit-pack.html", "text/html", audit_pack.build_html(pack).encode()),
            (f"STR-{case['ref']}-goaml.xml.txt", "text/plain", filing["payload"].encode())], "SAR_STR_FILING", desc)
        _tz(cms.comment, cms_case, task_id, f"Attested STR attached ({draft_id}, content hash {d['content_hash'][:16]}…). "
                                            "Any later change to the narrative or its evidence fails integrity verification.")
        with db.session() as conn:
            append_trail(conn, d["case_id"], "integration", user["email"], "TAZAMA_CMS_EVIDENCE_PUSHED",
                         {"cms_case_id": cms_case, "cms_task_id": task_id, "cms_evidence_ids": ids,
                          "content_hash": d["content_hash"], "pack_hash": pack["pack_hash"]}, draft_id)
        out["evidence_ids"] = ids
    ref = (filing or {}).get("fiu_reference")
    if ref and not completed:
        _tz(cms.comment, cms_case, task_id, f"Filed with the FMU via goAML. FIU reference {ref}.")
        _tz(cms.call, "POST", f"/api/v1/task/{task_id}/complete")
        with db.session() as conn:
            append_trail(conn, d["case_id"], "integration", user["email"], "TAZAMA_CMS_TASK_COMPLETED",
                         {"cms_case_id": cms_case, "cms_task_id": task_id, "fiu_reference": ref}, draft_id)
        out["task_completed"] = True
    out["status"] = "completed" if (ref or completed) else "evidence attached; record the FIU reference to close the CMS task"
    return out
