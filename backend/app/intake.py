"""Case Assembler (agent 0, deterministic): case bundle → Evidence Ledger.

A Tazama alert is split into atomic evidence items (the alert header, one
item per rule result) rather than stored as one opaque blob, so a sentence can
cite "rule 045 fired" rather than "the case file". Transactions arrive one per
item, as the CMS gold-lakehouse query would return them; the query and its
parameters are recorded in source_ref so the lookup is reproducible.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from app import chain
from app.db import dumps
from app.ledger import append_evidence, append_trail
from app.templates import load_template


def ingest_case(conn: sqlite3.Connection, bundle: dict[str, Any], tenant_id: str, actor: str = "system:intake",
                refresh: bool = False) -> str:
    """Create the case and its ledger. With refresh=True the case already exists:
    items already in the ledger (same type, source reference and content) are
    skipped and only new or changed source records are appended, so a re-import
    never rewrites history."""
    c = bundle["case"]
    case_id = c["id"]
    subject = bundle["subject"]
    alert = bundle["alert"]
    tmpl = load_template(c["jurisdiction"])
    prov = bundle.get("provenance") or {}
    src = lambda kind, default: prov.get(kind) or default  # noqa: E731

    existing = {(r["type"], r["source_ref"], r["content_hash"]) for r in conn.execute(
        "SELECT type, source_ref, content_hash FROM evidence WHERE case_id=?", (case_id,)).fetchall()} if refresh else set()

    def add(etype: str, label: str, source: str, source_ref: str, content: dict[str, Any], who: str) -> dict[str, Any]:
        if (etype, source_ref, chain.content_hash(content)) in existing:
            return {"id": None}
        return append_evidence(conn, case_id, etype, label, source, source_ref, content, who)

    if not refresh:
        conn.execute(
            "INSERT INTO cases (id, ref, tenant_id, title, summary, jurisdiction, subject_json, alert_json, status,"
            " opened_at, detected_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (case_id, c["ref"], tenant_id, c["title"], c["summary"], c["jurisdiction"],
             dumps({"name": subject["name"], "account": subject["account"], "occupation": subject.get("occupation") or "Not on record",
                    "risk_rating": subject.get("risk_rating") or "Not rated"}),
             dumps({"alertId": alert["alertId"], "typology": alert["report"]["tadpResult"]["typologyResult"][0]["cfg"],
                    "score": alert["report"]["tadpResult"]["typologyResult"][0]["result"],
                    "threshold": alert["report"]["tadpResult"]["typologyResult"][0]["workflow"]["alertThreshold"]}),
             "INVESTIGATING", c["opened_at"], c["detected_at"]))

    ids: list[str] = []

    # Reference data the narrative may rely on (the reporting threshold) is
    # itself evidence, so "just below the threshold" has lineage like any fact.
    ref = tmpl["reference"]
    ids.append(add("reference", "Cash transaction reporting threshold",
                               f"template:{tmpl['id']}@{tmpl['version']}", ref["source"],
                               {"ctr_threshold": ref["ctr_threshold"], "currency": ref["currency"],
                                "near_threshold_band_pct": ref["near_threshold_band_pct"],
                                "note": ref["note"]}, actor)["id"])

    tr = alert["report"]["tadpResult"]["typologyResult"][0]
    a_src, a_ref = src("alert", ("tazama:relay-service", f"alert/{alert['alertId']}"))
    ids.append(add("tazama_alert", f"Tazama alert {alert['alertId']}",
                               a_src, a_ref,
                               {"alertId": alert["alertId"], "timestamp": alert["timestamp"],
                                "evaluationID": alert["report"]["evaluationID"], "typology": tr["cfg"],
                                "score": tr["result"], "alertThreshold": tr["workflow"]["alertThreshold"],
                                "interdictionThreshold": tr["workflow"]["interdictionThreshold"],
                                "trigger": alert["transaction"]["FIToFIPmtSts"]["TxInfAndSts"]}, actor)["id"])
    for rr in tr["ruleResults"]:
        ids.append(add("rule_result", f"Rule {rr['id']} {rr['subRuleRef']}",
                                   "tazama:rule-processor", f"alert/{alert['alertId']}/rule/{rr['id']}",
                                   {**rr, "typology": tr["cfg"]}, actor)["id"])

    k_src, k_ref = src("kyc", ("cms:customer-profile", f"customer/{subject['account']}"))
    ids.append(add("kyc", "KYC profile", k_src, k_ref, subject, actor)["id"])
    if bundle.get("screening"):
        s_src, s_ref = src("screening", ("screening:internal", f"screening/{case_id}"))
        ids.append(add("screening", "Sanctions / PEP / adverse-media screening",
                                   s_src, s_ref, bundle["screening"], actor)["id"])

    txns = bundle["transactions"]
    q = prov.get("query") or {"tool": "lakehouse.transactions", "entity": subject["account"],
                              "window": [txns[0]["booked_at"][:10], txns[-1]["booked_at"][:10]]}
    q_hash = chain.content_hash(q)[:16]
    t_src, t_ref = src("transactions", ("cms:gold-lakehouse", f"lakehouse.transactions?query={q_hash}#"))
    for t in txns:
        label = f"{t['channel'].replace('_', ' ').title()} {'credit' if t['direction'] == 'in' else 'debit'} {t['txn_id']}"
        ids.append(add("transaction", label, t_src, f"{t_ref}{t['txn_id']}", t, actor)["id"])

    n_src, n_ref = src("notes", ("cms:case-notes", f"case/{case_id}/note/"))
    for n, note in enumerate(bundle["notes"], 1):
        ids.append(add("case_note", f"Case note {n} ({note['author']})",
                                   n_src, f"{n_ref}{note.get('cms_comment_id', n)}", note, actor)["id"])

    ids = [i for i in ids if i]
    append_trail(conn, case_id, "system", actor, "CASE_REFRESHED" if refresh else "CASE_INGESTED",
                 {"evidence_items": len(ids), "new_evidence": ids if refresh else None, "lakehouse_query": q, "query_hash": q_hash,
                  "alert_id": alert["alertId"]})
    return case_id
