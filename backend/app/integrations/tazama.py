"""Tazama connector: a real Tazama deployment in, an attested STR back out.

Reads, with no fixtures:
  * the case, its alert and the investigators' comments from the Tazama CMS API;
  * the KYC and screening records the investigator attached to the case in the
    CMS (evidence types KYC and SANCTIONS);
  * every payment to or from the subject account from Tazama's own message
    store (raw_history.pacs008 / pacs002), the ISO 20022 messages exactly as
    the switch sent them;
  * the human-readable reason for each rule result from Tazama's configuration
    database (the rule's own band definitions).

Writes back to the CMS "SAR/STR Filing" task: the audit pack and goAML STR as
SAR_STR_FILING evidence, a comment carrying the attested content hash, and
task completion once an FIU reference is recorded.

Tazama does not hold KYC, so if the investigator attached none the subject is
described only from the payment messages, and the profile red flags cannot be
evaluated. The pipeline then reports the gap or abstains rather than guessing.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings

SAR_TASK = "SAR/STR Filing"
BRIDGE_TAG = "[SAR Studio]"


class TazamaError(Exception):
    pass


def enabled() -> bool:
    return bool(settings.tazama_cms_url and settings.tazama_cms_user and settings.tazama_pg_dsn)


def _sha256(doc: Any) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def mask(acct: str, agent: str = "") -> str:
    return f"{agent.upper() + ' ' if agent else ''}wallet ****{acct[-4:]}"


# ── CMS API ──────────────────────────────────────────────────────────────────

class Cms:
    def __init__(self) -> None:
        self.base = settings.tazama_cms_url.rstrip("/")
        body = json.dumps({"username": settings.tazama_cms_user, "password": settings.tazama_cms_password}).encode()
        req = urllib.request.Request(f"{self.base}/v1/auth/login", data=body, method="POST",
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                self.token = json.load(r)["token"]
        except (urllib.error.URLError, KeyError) as e:
            raise TazamaError(f"Could not sign in to the Tazama CMS at {self.base}: {e}") from None
        p = self.token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
        self.user_id = claims.get("clientId")
        self.tenant = claims.get("tenantId") or "DEFAULT"

    def call(self, method: str, path: str, body: Any = None, raw: bool = False) -> Any:
        req = urllib.request.Request(f"{self.base}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"authorization": f"Bearer {self.token}", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            raise TazamaError(f"CMS {method} {path} → HTTP {e.code}: {e.read()[:300].decode(errors='replace')}") from None
        if raw:
            return data
        return json.loads(data) if data else None

    def upload(self, task_id: int, files: list[tuple[str, str, bytes]], evidence_type: str, description: str) -> list[str]:
        boundary = uuid.uuid4().hex
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                 for k, v in (("taskId", str(task_id)), ("evidenceType", evidence_type), ("description", description))]
        for name, ctype, data in files:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
                         f'Content-Type: {ctype}\r\n\r\n'.encode() + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(f"{self.base}/api/v1/evidence/upload", data=b"".join(parts), method="POST",
                                     headers={"authorization": f"Bearer {self.token}",
                                              "content-type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            raise TazamaError(f"CMS evidence upload → HTTP {e.code}: {e.read()[:300].decode(errors='replace')}") from None
        items = out if isinstance(out, list) else (out.get("data") or out.get("evidence") or [out])
        return [str(i.get("id") or i.get("evidence_id")) for i in items if isinstance(i, dict)]

    def comment(self, case_id: int, task_id: int, note: str) -> None:
        self.call("POST", "/api/v1/comment", {"caseId": case_id, "taskId": task_id, "tenantId": self.tenant,
                                              "note": f"{BRIDGE_TAG} {note}"[:500]})

    def sar_tasks(self) -> list[dict[str, Any]]:
        """Open SAR/STR Filing tasks on confirmed cases, newest first."""
        res = self.call("GET", "/api/v1/cases/all?limit=500")
        out = []
        for c in res.get("cases", []):
            for t in c.get("tasks", []):
                if t["name"] == SAR_TASK and t["status"] != "STATUS_30_COMPLETED":
                    out.append({"case": c, "task": t})
        return sorted(out, key=lambda x: -x["case"]["case_id"])


# ── Tazama's own stores ──────────────────────────────────────────────────────

def _pg(dbname: str):
    import psycopg  # imported lazily: the connector is optional
    try:
        return psycopg.connect(f"{settings.tazama_pg_dsn.rstrip('/')}/{dbname}", connect_timeout=10)
    except psycopg.Error as e:
        raise TazamaError(f"Could not reach Tazama's database {dbname}: {e}") from None


def _rule_reasons(tenant: str, rules: list[dict[str, Any]]) -> dict[str, str]:
    """Reason text for each (rule id, sub-rule ref), from the rule's own band config."""
    out: dict[str, str] = {}
    with _pg("configuration") as conn:
        for r in rules:
            row = conn.execute("SELECT configuration FROM rule WHERE ruleid=%s AND tenantid=%s", (r["id"], tenant)).fetchone()
            if not row:
                continue
            cfg = row[0]
            desc = cfg.get("desc") or r["id"]
            outcomes = (cfg.get("config") or {}).get("bands") or (cfg.get("config") or {}).get("cases") or []
            reason = next((b.get("reason") for b in outcomes if b.get("subRuleRef") == r.get("subRuleRef")), None)
            out[f"{r['id']}{r.get('subRuleRef', '')}"] = f"{desc}: {reason}" if reason else desc
    return out


def _payment(doc: dict[str, Any]) -> dict[str, Any]:
    x = doc["FIToFICstmrCdtTrf"]["CdtTrfTxInf"]
    g = doc["FIToFICstmrCdtTrf"]["GrpHdr"]
    acct = lambda k: x[k]["Id"]["Othr"][0]["Id"]  # noqa: E731
    return {
        "e2e": x["PmtId"]["EndToEndId"], "msg_id": g["MsgId"], "at": g["CreDtTm"],
        "amount": float(x["IntrBkSttlmAmt"]["Amt"]["Amt"]), "ccy": x["IntrBkSttlmAmt"]["Amt"]["Ccy"],
        "dbtr": {"name": x["Dbtr"].get("Nm", ""), "acct": acct("DbtrAcct"), "agent": x["DbtrAgt"]["FinInstnId"]["ClrSysMmbId"]["MmbId"],
                 "dob": x["Dbtr"]["Id"]["PrvtId"].get("DtAndPlcOfBirth", {}).get("BirthDt"), "mobile": x["Dbtr"].get("CtctDtls", {}).get("MobNb")},
        "cdtr": {"name": x["Cdtr"].get("Nm", ""), "acct": acct("CdtrAcct"), "agent": x["CdtrAgt"]["FinInstnId"]["ClrSysMmbId"]["MmbId"],
                 "dob": x["Cdtr"]["Id"]["PrvtId"].get("DtAndPlcOfBirth", {}).get("BirthDt"), "mobile": x["Cdtr"].get("CtctDtls", {}).get("MobNb")},
        "purpose": (x.get("Purp") or {}).get("Cd", ""), "memo": (doc["FIToFICstmrCdtTrf"].get("RmtInf") or {}).get("Ustrd", ""),
        "doc_sha256": _sha256(doc),
    }


def subject_history(tenant: str, e2e: str, until: datetime) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Pick the subject account of the alerted payment and load its history."""
    since = until - timedelta(days=settings.tazama_history_days)
    with _pg("raw_history") as conn:
        row = conn.execute("SELECT document FROM pacs008 WHERE endtoendid=%s AND tenantid=%s", (e2e, tenant)).fetchone()
        if not row:
            raise TazamaError(f"Tazama has no pacs.008 for EndToEndId {e2e} (tenant {tenant})")
        alerted = _payment(row[0])
        counts = {}
        for side in ("dbtr", "cdtr"):
            a = alerted[side]["acct"]
            counts[side] = conn.execute(
                "SELECT count(*) FROM pacs008 WHERE tenantid=%s AND (debtoraccountid=%s OR creditoraccountid=%s)",
                (tenant, a, a)).fetchone()[0]
        side = "cdtr" if counts["cdtr"] >= counts["dbtr"] else "dbtr"
        subj = alerted[side]
        rows = conn.execute(
            "SELECT document FROM pacs008 WHERE tenantid=%s AND (debtoraccountid=%s OR creditoraccountid=%s)"
            " AND credttm >= %s AND credttm <= %s ORDER BY credttm",
            (tenant, subj["acct"], subj["acct"], since.strftime("%Y-%m-%dT%H:%M:%S"), until.strftime("%Y-%m-%dT%H:%M:%S.999Z"))).fetchall()
        pays = [_payment(r[0]) for r in rows]
        ids = [p["e2e"] for p in pays]
        sts = dict(conn.execute(
            "SELECT document->'FIToFIPmtSts'->'TxInfAndSts'->>'OrgnlEndToEndId', document->'FIToFIPmtSts'->'TxInfAndSts'->>'TxSts'"
            " FROM pacs002 WHERE tenantid=%s AND document->'FIToFIPmtSts'->'TxInfAndSts'->>'OrgnlEndToEndId' = ANY(%s)",
            (tenant, ids)).fetchall()) if ids else {}
    for p in pays:
        p["status"] = sts.get(p["e2e"], "NO_PACS002")
    query = {"tool": "tazama.raw_history.pacs008", "tenant": tenant, "account": subj["acct"],
             "window": [since.isoformat(timespec="seconds"), until.isoformat(timespec="seconds")]}
    return subj, pays, query


# ── Investigator attachments (KYC, screening) ────────────────────────────────

def _parse_kv(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = re.match(r"\s*([A-Za-z][A-Za-z /()_-]{1,60}?)\s*[:=]\s*(.+?)\s*$", line)
        if m:
            out[re.sub(r"[^a-z]+", "_", m.group(1).lower()).strip("_")] = m.group(2)
    return out


def _num(v: str | None) -> float | None:
    if not v:
        return None
    m = re.search(r"[\d,]+(\.\d+)?", v)
    return float(m.group(0).replace(",", "")) if m else None


def _date(v: str | None) -> str | None:
    if not v:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(v.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def attachments(cms: Cms, case_id: int, task_ids: list[int]) -> dict[str, list[dict[str, Any]]]:
    """Text attachments of type KYC / SANCTIONS on the case's tasks, parsed as key: value lines."""
    found: dict[str, list[dict[str, Any]]] = {"KYC": [], "SANCTIONS": []}
    for tid in task_ids:
        res = cms.call("GET", f"/api/v1/evidence/task/{tid}") or {}
        for ev in res.get("evidence", []) if isinstance(res, dict) else []:
            et = ev.get("evidenceType")
            if et not in found:
                continue
            for att in ev.get("attachments", []):
                if not att.get("fileName", "").endswith(".txt"):
                    continue
                q = urllib.parse.urlencode({"attachmentName": att["fileName"]})
                text = cms.call("GET", f"/api/v1/evidence/{ev['id']}/download?{q}", raw=True).decode("utf-8", "replace")
                found[et].append({"evidence_id": ev["id"], "file": att["fileName"], "cms_hash": att.get("hash"),
                                  "uploaded_at": ev.get("uploadedAt"), "fields": _parse_kv(text), "text": text})
    return found


# ── Bundle for the Case Assembler ────────────────────────────────────────────

def build_bundle(cms: Cms, cms_case_id: int) -> dict[str, Any]:
    c = cms.call("GET", f"/api/v1/cases/{cms_case_id}")
    if c.get("status") != "STATUS_82_CLOSED_CONFIRMED":
        raise TazamaError(f"CMS case {cms_case_id} is {c.get('status')}; only confirmed cases have a SAR/STR task")
    a = c["alert"]
    tenant = c.get("tenant_id") or cms.tenant
    rep = a["alert_data"]
    tr = rep["tadpResult"]["typologyResult"][0]
    sts = a["transaction"]["FIToFIPmtSts"]["TxInfAndSts"]
    e2e = sts["OrgnlEndToEndId"]
    alert_ts = datetime.fromisoformat(rep["timestamp"].replace("Z", "+00:00"))
    reasons = _rule_reasons(tenant, tr["ruleResults"])
    rules = [r for r in tr["ruleResults"] if r["id"] != tr.get("workflow", {}).get("flowProcessor")]

    subj, pays, query = subject_history(tenant, e2e, alert_ts)
    tasks = cms.call("GET", f"/api/v1/task/case/{cms_case_id}") or []
    sar_task = next((t for t in sorted(tasks, key=lambda t: -t["task_id"]) if t["name"] == SAR_TASK), None)
    att = attachments(cms, cms_case_id, [t["task_id"] for t in tasks])

    kyc_src = att["KYC"][-1] if att["KYC"] else None
    k = kyc_src["fields"] if kyc_src else {}
    subject = {
        "name": k.get("name") or subj["name"], "account": mask(subj["acct"], subj["agent"]),
        "occupation": k.get("occupation"), "employer": k.get("employer"),
        "declared_monthly_income": _num(k.get("declared_monthly_income") or k.get("monthly_income")),
        "declared_monthly_turnover": _num(k.get("declared_monthly_turnover")),
        "account_opened": _date(k.get("account_opened")), "city": k.get("city", ""),
        "dob": _date(k.get("date_of_birth")) or subj.get("dob"), "risk_rating": k.get("risk_rating", "Not rated"),
        "customer_type": k.get("customer_type", "individual"),
        "tazama_account_id": subj["acct"], "tazama_agent": subj["agent"],
        "kyc_source": (f"Tazama CMS evidence {kyc_src['evidence_id']} ({kyc_src['file']}, CMS hash {kyc_src['cms_hash']})"
                       if kyc_src else "No KYC record attached in the CMS; identity from the ISO 20022 payment messages only"),
    }
    sc = att["SANCTIONS"][-1] if att["SANCTIONS"] else None
    screening = None
    if sc:
        f = sc["fields"]
        norm = lambda v: "NO_MATCH" if re.search(r"no\s*match|clear|none|negative", (v or "").lower()) else (v or "UNKNOWN").upper()  # noqa: E731
        screening = {"provider": f.get("provider", "Investigator-attached screening"),
                     "screened_at": _date(f.get("screened_at") or f.get("date")) or (sc["uploaded_at"] or "")[:10],
                     "sanctions": norm(f.get("sanctions")), "pep": norm(f.get("pep")),
                     "adverse_media": norm(f.get("adverse_media")),
                     "source": f"Tazama CMS evidence {sc['evidence_id']} ({sc['file']})"}

    txns = []
    for p in pays:
        incoming = p["cdtr"]["acct"] == subj["acct"]
        other = p["dbtr"] if incoming else p["cdtr"]
        txns.append({
            "txn_id": p["e2e"], "booked_at": p["at"].replace("Z", "+00:00"), "direction": "in" if incoming else "out",
            "channel": "WALLET_TRANSFER", "amount": p["amount"], "currency": p["ccy"],
            "counterparty_name": other["name"], "counterparty_account": mask(other["acct"], other["agent"]),
            "counterparty_bank": other["agent"], "counterparty_country": "PK", "branch": "", "city": "",
            "memo": p["memo"], "purpose": p["purpose"], "settlement_status": p["status"],
            "tazama_msg_id": p["msg_id"], "tazama_doc_sha256": p["doc_sha256"],
        })

    # The CMS API exposes user IDs, not names, so authors are labelled by role
    # from what the case itself records.
    sar_ids = {sar_task["task_id"]} if sar_task else set()
    notes = []
    for cm in sorted(cms.call("GET", f"/api/v1/comment/case/{cms_case_id}/comment") or [], key=lambda x: x["created_at"]):
        text = (cm.get("note") or "").strip()
        if not text or text.startswith(BRIDGE_TAG) or len(text) < 12:
            continue
        uid = cm["user_id"]
        if text.startswith("Supervisor Approval"):
            role = "Supervisor"
        elif cm.get("task_id") in sar_ids:
            role = "Compliance officer"
        elif uid == c.get("case_owner_user_id"):
            role = "Assigned investigator"
        else:
            role = "CMS user"
        notes.append({"author": f"{role} (CMS user {uid[:8]})", "at": cm["created_at"].replace("Z", "+00:00"),
                      "text": text, "cms_comment_id": cm["comment_id"]})

    # Investigation notes written on tasks in the CMS UI (one finding per paragraph).
    for t in tasks:
        raw = re.sub(r"<[^>]+>", "\n", t.get("investigationNotes") or "")
        for k, para in enumerate(p.strip() for p in re.split(r"\n\s*\n", raw)):
            para = re.sub(r"^\s*Completion Notes:\s*", "", " ".join(para.split()))
            if len(para) < 12:
                continue
            who = "Assigned investigator" if t.get("assigned_user_id") == c.get("case_owner_user_id") else "Investigator"
            notes.append({"author": f"{who} (CMS task {t['task_id']}, investigation notes)",
                          "at": (t.get("updated_at") or t["created_at"]).replace("Z", "+00:00"),
                          "text": para, "cms_comment_id": f"task-{t['task_id']}-note-{k + 1}"})
    notes.sort(key=lambda n: n["at"])

    ref = f"TZ-CMS-{cms_case_id}"
    return {
        "case": {"id": f"TZ-{cms_case_id}", "ref": ref,
                 "title": f"Tazama case {cms_case_id}: {c.get('case_type') or 'AML'} alert on {subject['name']}",
                 "summary": (f"Imported from the Tazama CMS (case {cms_case_id}, alert {a['alert_id']}). "
                             f"{len(txns)} payments to or from the subject's account in the {settings.tazama_history_days} days "
                             f"before the alert, read from Tazama's message store."),
                 "jurisdiction": "GOAML:PK", "detected_at": rep["timestamp"].replace("Z", "+00:00"),
                 "opened_at": c["created_at"].replace("Z", "+00:00")},
        "subject": subject,
        "alert": {
            "alertId": f"CMS-ALERT-{a['alert_id']}", "tenantId": tenant, "status": rep.get("status", "ALRT"),
            "timestamp": rep["timestamp"].replace("Z", "+00:00"),
            "report": {"evaluationID": rep.get("evaluationID", ""), "status": rep.get("status", "ALRT"),
                       "timestamp": rep["timestamp"],
                       "tadpResult": {"id": rep["tadpResult"]["id"], "cfg": rep["tadpResult"]["cfg"], "typologyResult": [{
                           "id": tr["id"], "cfg": tr["cfg"], "result": tr["result"], "review": tr.get("review"),
                           "workflow": {"alertThreshold": tr["workflow"]["alertThreshold"],
                                        "interdictionThreshold": tr["workflow"].get("interdictionThreshold")},
                           "ruleResults": [{"id": r["id"], "cfg": r["cfg"], "subRuleRef": r["subRuleRef"], "wght": r["wght"],
                                            "reason": reasons.get(f"{r['id']}{r['subRuleRef']}", r["id"])} for r in rules]}]}},
            "transaction": a["transaction"],
        },
        "transactions": txns,
        "screening": screening,
        "notes": notes,
        "provenance": {
            "kyc": ("tazama-cms:evidence", kyc_src["evidence_id"]) if kyc_src else ("tazama:raw_history.pacs008", f"party/{subj['acct']}"),
            "screening": ("tazama-cms:evidence", sc["evidence_id"]) if sc else None,
            "transactions": ("tazama:raw_history.pacs008", "pacs008?e2e="),
            "notes": ("tazama-cms:comments", f"case/{cms_case_id}/comment/"),
            "alert": ("tazama-cms:alert", f"alert/{a['alert_id']}"),
            "query": query,
        },
        "cms": {"case_id": cms_case_id, "task_id": sar_task["task_id"] if sar_task else None, "e2e": e2e,
                "alert_id": a["alert_id"], "tenant": tenant},
    }
