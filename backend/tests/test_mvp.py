"""End-to-end tests through the HTTP API, plus direct tamper and model-path tests."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import seed
from app import audit
from app.config import settings
from app.main import app
from app.pipeline import drafter, gateway

BACKEND = Path(__file__).resolve().parent.parent


def reseed() -> None:
    old = sys.argv
    sys.argv = ["seed.py", "--reset"]
    try:
        seed.main()
    finally:
        sys.argv = old


@pytest.fixture()
def client():
    reseed()
    with TestClient(app) as c:
        yield c


def login(c: TestClient, who: str) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"email": f"{who}@lumous.demo", "password": seed.PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def generate(c: TestClient, h: dict, case: str, **kw) -> dict:
    r = c.post(f"/api/cases/{case}/drafts", json=kw, headers=h)
    assert r.status_code == 202, r.text
    did = r.json()["id"]
    for _ in range(200):
        p = c.get(f"/api/drafts/{did}/progress", headers=h).json()
        if p["status"] not in ("REQUESTED", "GENERATING"):
            break
        time.sleep(0.05)
    return c.get(f"/api/drafts/{did}", headers=h).json()


def decide(c: TestClient, h: dict, case: str) -> None:
    r = c.post(f"/api/cases/{case}/decision", headers=h, json={
        "sar_required": True, "reason": "Pattern of cash deposits just under the CTR threshold across branches.",
        "suspicion_formed_at": "2026-09-01T10:00:00"})
    assert r.status_code == 200, r.text


# ── Pipeline outcomes ────────────────────────────────────────────────────────

def test_login_rejects_wrong_password(client):
    assert client.post("/api/auth/login", json={"email": "ayesha@lumous.demo", "password": "x"}).status_code == 401
    assert client.get("/api/cases").status_code == 401


def test_three_cases_expected_outcomes(client):
    h = login(client, "ayesha")
    a = generate(client, h, "CASE-1001")
    b = generate(client, h, "CASE-1002")
    t = generate(client, h, "CASE-1003")
    assert a["draft"]["status"] == "NEEDS_REVIEW" and b["draft"]["status"] == "NEEDS_REVIEW"
    for d in (a, b):
        assert d["draft"]["stats"]["removed"] == 0
        assert all(s["verdict"]["status"] == "supported" for s in d["sentences"])
        assert all(c["status"] == "met" for c in d["draft"]["checklist"])
        assert all(s["cites"] for s in d["sentences"])
    assert t["draft"]["status"] == "ABSTAINED"
    assert t["draft"]["abstain"]["decision"] == "INSUFFICIENT_GROUNDS"
    assert t["sentences"] == []
    v = client.get("/api/cases/CASE-1003/verify", headers=h).json()
    assert v["verdict"] == "PASS", v["problems"]  # a declined draft has no narrative hash to mismatch


def test_injected_faults_are_caught(client):
    h = login(client, "ayesha")
    d = generate(client, h, "CASE-1001", inject_faults=True)
    removed = d["draft"]["removed"]
    codes = {p["code"] for r in removed for p in r["problems"]}
    assert {"H3", "S1", "A1", "P1"} <= codes
    texts = " ".join(s["text"] for s in d["sentences"])
    assert "Crescent Holdings" not in texts and "will be informed" not in texts and "knowingly" not in texts
    assert "24,346,000" not in texts  # the +250,000 numeric fault was repaired
    assert d["draft"]["stats"]["repaired"] >= 3
    assert all(s["verdict"]["status"] != "failed" for s in d["sentences"])


def test_prompt_injection_is_flagged_not_obeyed(client):
    h = login(client, "ayesha")
    d = generate(client, h, "CASE-1002")
    flags = d["draft"]["stats"]["injection_flags"]
    assert len(flags) == 1 and flags[0]["field"] == "memo"
    assert "low risk" not in " ".join(s["text"] for s in d["sentences"]).lower()


# ── Review and maker/checker ─────────────────────────────────────────────────

def _submitted(client) -> tuple[dict, dict, str]:
    h = login(client, "ayesha")
    d = generate(client, h, "CASE-1001")
    did = d["draft"]["id"]
    decide(client, h, "CASE-1001")
    assert client.post(f"/api/drafts/{did}/accept-supported", headers=h).json()["accepted"] == len(d["sentences"])
    r = client.post(f"/api/drafts/{did}/submit", headers=h)
    assert r.status_code == 200, r.text
    return h, r.json(), did


def test_edit_with_unsupported_number_is_refused(client):
    h = login(client, "ayesha")
    d = generate(client, h, "CASE-1001")
    s = next(x for x in d["sentences"] if "cash deposits totalling" in x["text"] and x["section"] == "activity")
    bad = s["text"].replace("PKR 24,096,000", "PKR 25,000,000")
    assert bad != s["text"]
    r = client.patch(f"/api/drafts/{d['draft']['id']}/sentences/{s['id']}", headers=h,
                     json={"action": "edit", "reason_code": "EDIT_FACTUAL", "text": bad})
    assert r.status_code == 422
    assert any(p["code"] == "H1" for p in r.json()["problems"])
    ok = client.patch(f"/api/drafts/{d['draft']['id']}/sentences/{s['id']}", headers=h,
                      json={"action": "edit", "reason_code": "EDIT_WORDING", "text": s["text"].replace("made over the counter", "deposited over the counter")})
    assert ok.status_code == 200, ok.text
    conn = sqlite3.connect(settings.db_path)
    refused = conn.execute("SELECT COUNT(*) FROM trail WHERE action='EDIT_REFUSED' AND draft_id=?", (d["draft"]["id"],)).fetchone()[0]
    conn.close()
    assert refused == 1, "a refused edit must stay in the audit trail"


def test_submit_requires_decision_and_review(client):
    h = login(client, "ayesha")
    d = generate(client, h, "CASE-1001")
    r = client.post(f"/api/drafts/{d['draft']['id']}/submit", headers=h)
    assert r.status_code == 409
    assert any("SAR decision" in p for p in r.json()["problems"])
    assert any("not yet reviewed" in p for p in r.json()["problems"])


def test_maker_checker_and_filing(client):
    h, sub, did = _submitted(client)
    # The analyst cannot approve at all (role), the MLRO can.
    assert client.post(f"/api/drafts/{did}/approve", headers=h, json={"attestation": "x" * 30, "content_hash": sub["content_hash"]}).status_code == 403
    m = login(client, "bilal")
    stale = client.post(f"/api/drafts/{did}/approve", headers=m, json={"attestation": "I have reviewed each claim.", "content_hash": "0" * 64})
    assert stale.status_code == 409
    ok = client.post(f"/api/drafts/{did}/approve", headers=m,
                     json={"attestation": "I have reviewed each claim against its evidence and approve filing.", "content_hash": sub["content_hash"]})
    assert ok.status_code == 200, ok.text
    ex = client.post(f"/api/drafts/{did}/export/goaml", headers=m).json()
    assert ex["validation"]["valid"], ex["validation"]
    assert "<report_code>STR</report_code>" in ex["xml"] and ex["transactions"] >= 14
    f = client.post(f"/api/drafts/{did}/filed", headers=h, json={"fiu_reference": "FMU-STR-2026-000123"})
    assert f.status_code == 200 and f.json()["status"] == "FILED"
    pack = client.get(f"/api/drafts/{did}/audit-pack?format=json", headers=login(client, "sara")).json()
    assert pack["pack_hash"] and pack["A6_A7_integrity"]["verdict"] == "PASS"
    assert pack["draft"]["checker"].startswith("Bilal")
    html = client.get(f"/api/drafts/{did}/audit-pack", headers=m)
    assert html.status_code == 200 and "audit pack" in html.text


def test_mlro_who_reviewed_cannot_approve(client):
    """An MLRO who touched the draft during review is a maker for that draft."""
    h, sub, did = _submitted(client)
    m = login(client, "bilal")
    client.post(f"/api/drafts/{did}/return", headers=m, json={"reason": "Clarify the flow of funds."})
    conn = sqlite3.connect(settings.db_path)
    bilal_id = conn.execute("SELECT id FROM users WHERE email='bilal@lumous.demo'").fetchone()[0]
    conn.execute("UPDATE drafts SET status='PENDING_APPROVAL', maker_id=? WHERE id=?", (bilal_id, did))
    conn.commit()
    conn.close()
    r = client.post(f"/api/drafts/{did}/approve", headers=m,
                    json={"attestation": "Attesting to my own submission here.", "content_hash": sub["content_hash"]})
    assert r.status_code == 403
    conn = sqlite3.connect(settings.db_path)
    refused = conn.execute("SELECT COUNT(*) FROM trail WHERE action='APPROVAL_REFUSED' AND draft_id=?", (did,)).fetchone()[0]
    conn.close()
    assert refused == 1, "a refused self-approval must stay in the audit trail"


def test_audit_pack_is_complete_for_independent_audit(client):
    """Pack 1.1 carries the whole evidence chain and fact table, the allow-lists and the release (E7)."""
    h, sub, did = _submitted(client)
    m = login(client, "bilal")
    client.post(f"/api/drafts/{did}/approve", headers=m,
                json={"attestation": "I have reviewed each claim against its evidence and approve filing.", "content_hash": sub["content_hash"]})
    pack = client.get(f"/api/drafts/{did}/audit-pack?format=json", headers=login(client, "sara")).json()
    assert pack["pack_version"] == "1.1" and pack["facts_scope"] == "full" and pack["evidence_scope"] == "full"
    assert [e["seq"] for e in pack["evidence"]] == list(range(1, len(pack["evidence"]) + 1))
    assert pack["outline"]["slots"] and pack["actors"]
    typ = next(t for t in pack["A3_A4_trail"] if t["action"] == "AGENT_RUN" and t["detail"]["agent"] == "typology_analyst")
    assert typ["detail"]["abstain_confidence"] == settings.abstain_confidence
    assert all({"support", "counter"} <= set(e) for e in typ["detail"]["evaluated"])


def test_returned_draft_can_be_approved_by_same_mlro(client):
    h, sub, did = _submitted(client)
    m = login(client, "bilal")
    assert client.post(f"/api/drafts/{did}/return", headers=m, json={"reason": "Clarify the flow of funds."}).status_code == 200
    again = client.post(f"/api/drafts/{did}/submit", headers=h)
    assert again.status_code == 200, again.text
    ok = client.post(f"/api/drafts/{did}/approve", headers=m,
                     json={"attestation": "Reviewed again after the return; approve.", "content_hash": again.json()["content_hash"]})
    assert ok.status_code == 200, ok.text


def test_auditor_is_read_only(client):
    s = login(client, "sara")
    assert client.post("/api/cases/CASE-1001/drafts", json={}, headers=s).status_code == 403
    assert client.get("/api/cases/CASE-1001/verify", headers=s).status_code == 200


# ── Integrity: the tamper demo ───────────────────────────────────────────────

def _tamper(*args: str) -> None:
    subprocess.run([sys.executable, "tamper.py", *args], cwd=BACKEND, check=True, capture_output=True,
                   env={"SAR_DB_PATH": str(settings.db_path), "PATH": "/usr/bin:/bin"})


def _deposit_id(case: str = "CASE-1001") -> str:
    conn = sqlite3.connect(settings.db_path)
    r = conn.execute("SELECT id FROM evidence WHERE case_id=? AND type='transaction' AND json_extract(content_json,'$.channel')='CASH_DEPOSIT'"
                     " ORDER BY seq LIMIT 1", (case,)).fetchone()
    conn.close()
    return r[0]


def test_app_cannot_update_evidence(client):
    conn = sqlite3.connect(settings.db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE evidence SET label='x' WHERE seq=1")
    conn.close()


@pytest.mark.parametrize("mode", [[], ["--rehash"]])
def test_tamper_detected_with_lineage_impact(client, mode):
    h = login(client, "ayesha")
    generate(client, h, "CASE-1001")
    assert client.get("/api/cases/CASE-1001/verify", headers=h).json()["verdict"] == "PASS"
    ev = _deposit_id()
    _tamper(ev, "--field", "amount", "--value", "1500000", *mode)
    rep = client.get("/api/cases/CASE-1001/verify", headers=h).json()
    assert rep["verdict"] == "FAIL"
    assert rep["chain_integrity"] == f"broken_at {ev}"
    d = rep["draft"]
    assert d["lineage_impact"]["facts"] and d["lineage_impact"]["sentences"]
    changed = {f["label"] for f in d["facts_not_reproducible"]}
    assert "Cash deposits" in changed
    assert d["auditability_score"] < 1


def test_full_chain_rewrite_caught_by_anchor(client):
    h = login(client, "ayesha")
    generate(client, h, "CASE-1001")
    _tamper(_deposit_id(), "--field", "amount", "--value", "1500000", "--rewrite-chain")
    rep = audit.verify_case("CASE-1001", None, "test", record=False)
    assert rep["evidence_chain"]["intact"]  # the attacker recomputed every hash...
    assert rep["draft"]["anchor"]["still_in_chain"] is False  # ...but the trail anchored the old head
    assert rep["verdict"] == "FAIL"


def test_sentence_tamper_breaks_attested_hash(client):
    h, sub, did = _submitted(client)
    m = login(client, "bilal")
    client.post(f"/api/drafts/{did}/approve", headers=m,
                json={"attestation": "I have reviewed each claim against its evidence.", "content_hash": sub["content_hash"]})
    conn = sqlite3.connect(settings.db_path)
    conn.execute("UPDATE sentences SET text = text || ' Extra.' WHERE draft_id=? AND idx=1", (did,))
    conn.commit()
    conn.close()
    rep = client.get(f"/api/cases/CASE-1001/verify?draft_id={did}", headers=m).json()
    assert rep["draft"]["content_hash"]["matches_attested"] is False
    assert "Draft no longer matches what the MLRO attested" in rep["problems"]


# ── Model path without a network: stub the gateway ───────────────────────────

def test_llm_drafter_path_with_pseudonymisation(client, monkeypatch):
    """Hosted endpoint: inputs are masked, outputs unmasked, bad output repaired then removed."""
    calls: list[str] = []

    def fake(provider, system, user):
        calls.append(user)
        data = json.loads(user.split("<case_data>\n", 1)[1].split("\n</case_data>", 1)[0])
        disp = {r["id"]: r["display"] for r in data}
        if "failed verification" in user:
            return {"sentences": [{"slot": "subject.identity", "text": f"The subject is {disp['F01']}, account {disp['F02']}.", "cites": ["F01", "F02"]}]}, {}
        return {"sentences": [
            {"slot": "subject.identity", "text": f"The subject is {disp['F01']} with PKR 99,999 hidden.", "cites": ["F01", "F02"]},
            {"slot": "activity.totals", "text": "Activity occurred.", "cites": ["F12"]},
        ]}, {"tokens_in": 10, "tokens_out": 5}

    monkeypatch.setattr(gateway, "complete_json", fake)
    monkeypatch.setattr(gateway, "available", lambda p: (True, "stub"))
    h = login(client, "ayesha")
    d = generate(client, h, "CASE-1001", provider="anthropic")
    assert d["draft"]["status"] == "NEEDS_REVIEW", d["draft"]["error"]
    assert "Faraz Mehmood" not in calls[0] and "[ENTITY_" in calls[0]  # masked before leaving
    first = d["sentences"][0]
    assert first["text"] == "The subject is Faraz Mehmood, account PK**MEZN****4411."  # unmasked, repaired
    assert first["origin"] == "repaired"


def test_offline_drafter_is_deterministic():
    reseed()
    from app import db
    from app.ledger import list_evidence
    from app.pipeline.facts import build_facts
    from app.pipeline.planner import plan
    from app.pipeline.typology import analyse
    from app.templates import load_template
    conn = db.connect()
    ev = list_evidence(conn, "CASE-1002")
    conn.close()
    outs = []
    for _ in range(2):
        ft = build_facts(ev)
        hy = analyse(ft, 0.6)
        outs.append(drafter.OfflineDrafter(ft.facts, hy).draft(plan(ft, hy, load_template("GOAML:PK"))))
    assert outs[0] == outs[1]


# ── Tazama CMS integration ───────────────────────────────────────────────────

def test_tazama_link_matches_alert_transaction_and_is_trailed(client):
    h = login(client, "ayesha")
    body = {"end_to_end_id": "TX1001-0014", "cms_case_id": "7", "cms_task_id": "21", "cms_url": "http://cms/cases/7"}
    r = client.post("/api/integrations/tazama/link", json=body, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["case_id"] == "CASE-1001" and r.json()["created"]
    assert client.post("/api/integrations/tazama/link", json=body, headers=h).json()["created"] is False
    assert client.get("/api/cases/CASE-1001", headers=h).json()["links"][0]["ext_case_id"] == "7"
    trail = client.get("/api/cases/CASE-1001/trail", headers=h).json()
    assert [t["action"] for t in trail].count("TAZAMA_CMS_LINKED") == 1
    assert client.get("/api/cases/CASE-1001/verify", headers=h).json()["verdict"] == "PASS"
    unknown = {**body, "end_to_end_id": "TX-NOPE"}
    assert client.post("/api/integrations/tazama/link", json=unknown, headers=h).status_code == 404
    assert client.post("/api/integrations/tazama/link", json=body, headers=login(client, "sara")).status_code == 403


def test_reimport_appends_only_new_evidence():
    from app import db
    from app.intake import ingest_case
    from app.synth import case_mule
    reseed()
    b = case_mule()
    with db.session() as conn:
        before = conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id='CASE-1002'").fetchone()[0]
        ingest_case(conn, b, settings.tenant_id, refresh=True)
        assert conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id='CASE-1002'").fetchone()[0] == before
        b["notes"].append({"author": "Investigator", "at": "2026-09-20T10:00:00+05:00", "text": "Account placed on hold."})
        ingest_case(conn, b, settings.tenant_id, refresh=True)
        assert conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id='CASE-1002'").fetchone()[0] == before + 1


def test_no_kyc_means_no_profile_claims(client):
    from app import db
    from app.intake import ingest_case
    from app.synth import case_mule
    b = case_mule()
    b["case"]["id"], b["case"]["ref"] = "CASE-9002", "TZ-TEST"
    b["subject"] = {"name": b["subject"]["name"], "account": b["subject"]["account"]}
    b["screening"] = None
    with db.session() as conn:
        ingest_case(conn, b, settings.tenant_id)
    h = login(client, "ayesha")
    d = generate(client, h, "CASE-9002")
    keys = {f["key"] for f in d["draft"]["facts"]}
    assert not keys & {"occupation", "declared_income", "income_multiple", "screening"}
    text = " ".join(s["text"] for s in d["sentences"])
    assert "declared occupation" not in text and "declared income" not in text
    if d["draft"]["status"] == "ABSTAINED":
        assert any("KYC" in m for m in d["draft"]["abstain"]["missing"])
