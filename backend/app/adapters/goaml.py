"""goAML STR export (filing adapter 8, deterministic, never calls a model).

Element names follow the UNODC goAML base report schema (report → reason,
action, transaction[t_from / t_to_my_client ...], report_indicators).
FIU-specific variants differ; no FMU XSD ships with this repository, so
`validate` performs structural and cross-field checks only (H7: the XML
must agree with the attested narrative and fact table). Load the FIU's XSD
before filing.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

SECTION_ORDER = ["subject", "activity", "indicators", "flow", "counter", "action"]


def narrative_text(sentences: list[dict[str, Any]], template: dict[str, Any]) -> str:
    titles = {s["id"]: s["title"] for s in template["sections"]}
    parts = []
    for sec in SECTION_ORDER:
        body = " ".join(s["text"] for s in sentences if s["status"] == "active" and s["section"] == sec)
        if body:
            parts.append(f"{titles[sec]}: {body}")
    return "\n\n".join(parts)


def _sub(parent: ET.Element, tag: str, text: Any = None) -> ET.Element:
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = str(text)
    return el


def build(case: dict[str, Any], draft: dict[str, Any], sentences: list[dict[str, Any]], evidence: list[dict[str, Any]],
          template: dict[str, Any], mlro: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    g = template["goaml"]
    facts = {f["id"]: f for f in draft["facts"]}
    active = [s for s in sentences if s["status"] == "active"]
    cited = {c for s in active for c in s["cites"]}
    # Transactions reported = those underlying any fact the narrative cites.
    ev_ids = {e for fid in cited if fid in facts for e in facts[fid]["derived_from"]}
    for h in draft["hypotheses"]:
        if h["id"] in cited:
            ev_ids |= {e for fid in h["support_fact_ids"] for e in facts[fid]["derived_from"]}
    txns = [e for e in evidence if e["type"] == "transaction" and e["id"] in ev_ids]
    kyc = next(e["content"] for e in evidence if e["type"] == "kyc")

    r = ET.Element("report")
    _sub(r, "rentity_id", g["rentity_id"])
    _sub(r, "rentity_branch", g["rentity_branch"])
    _sub(r, "submission_code", g["submission_code"])
    _sub(r, "report_code", g["report_code"])
    _sub(r, "entity_reference", case["ref"])
    _sub(r, "submission_date", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"))
    _sub(r, "currency_code_local", template["currency"])
    rp = _sub(r, "reporting_person")
    first, _, last = mlro["name"].partition(" ")
    _sub(rp, "first_name", first)
    _sub(rp, "last_name", last or first)
    _sub(rp, "occupation", "Money Laundering Reporting Officer")
    _sub(rp, "email", mlro["email"])
    loc = _sub(r, "location")
    _sub(loc, "address_type", "B")
    _sub(loc, "address", g["rentity_branch"])
    _sub(loc, "city", kyc.get("city", ""))
    _sub(loc, "country_code", "PK")
    _sub(r, "reason", narrative_text(sentences, template))
    _sub(r, "action", " ".join(s["text"] for s in active if s["section"] == "action") or "See narrative.")

    for e in txns:
        t = e["content"]
        tx = _sub(r, "transaction")
        _sub(tx, "transactionnumber", t["txn_id"])
        _sub(tx, "internal_ref_number", e["id"])
        _sub(tx, "transaction_location", ", ".join(x for x in (t.get("branch"), t.get("city")) if x) or "Digital channel")
        _sub(tx, "transaction_description", t["channel"].replace("_", " ").title() + (f" — {t['memo']}" if t.get("memo") else ""))
        _sub(tx, "date_transaction", t["booked_at"][:19])
        _sub(tx, "transmode_code", g["transmode_codes"].get(t["channel"], "E"))
        _sub(tx, "amount_local", f"{t['amount']:.2f}".removesuffix(".00"))
        client = {"institution_name": "Reporting entity", "account": kyc["account"], "name": kyc["name"]}
        other = {"institution_name": t.get("counterparty_bank") or "", "account": t.get("counterparty_account") or "",
                 "name": t.get("counterparty_name") or ""}
        if t["direction"] == "in":
            src, dst, src_tag, dst_tag = other, client, "t_from", "t_to_my_client"
        else:
            src, dst, src_tag, dst_tag = client, other, "t_from_my_client", "t_to"
        for tag, party, code_tag in ((src_tag, src, "from_funds_code"), (dst_tag, dst, "to_funds_code")):
            p = _sub(tx, tag)
            is_cash = t["channel"] in ("CASH_DEPOSIT", "ATM_WITHDRAWAL") and party is not client
            _sub(p, code_tag, "K" if is_cash else "A")
            if not is_cash:
                acc = _sub(p, "from_account" if tag.startswith("t_from") else "to_account")
                _sub(acc, "institution_name", party["institution_name"])
                _sub(acc, "account", party["account"])
                _sub(acc, "account_name", party["name"])
            _sub(p, "from_country" if tag.startswith("t_from") else "to_country", t.get("counterparty_country", "PK"))

    ind = _sub(r, "report_indicators")
    for h in draft["hypotheses"]:
        if h["supported"] and h["id"] in cited:
            _sub(ind, "indicator", h["typology_id"])
            for i in h["support"]:
                _sub(ind, "indicator", i["id"])
    ET.indent(r)
    return ET.tostring(r, encoding="unicode", xml_declaration=True), [e["content"] for e in txns]


REQUIRED = ["rentity_id", "submission_code", "report_code", "entity_reference", "submission_date",
            "currency_code_local", "reporting_person", "location", "reason", "action", "transaction", "report_indicators/indicator"]


def validate(xml: str, draft: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    checks = []
    try:
        root = ET.fromstring(xml.split("?>", 1)[-1])
    except ET.ParseError as e:
        return {"valid": False, "checks": [{"check": "well-formed XML", "ok": False, "detail": str(e)}], "note": template["goaml"]["schema_note"]}
    checks.append({"check": "well-formed XML", "ok": True, "detail": ""})
    for path in REQUIRED:
        ok = root.find(path) is not None and (root.find(path).text or len(root.find(path)))  # type: ignore[union-attr]
        checks.append({"check": f"<{path}> present", "ok": bool(ok), "detail": ""})
    reason = root.findtext("reason") or ""
    limit = template["narrative"]["max_chars"]
    checks.append({"check": f"reason ≤ {limit} characters", "ok": len(reason) <= limit, "detail": f"{len(reason)} characters"})
    # Cross-field consistency (H7): every reported amount appears in the ledger-derived facts' lineage.
    amounts = [float(t.findtext("amount_local") or 0) for t in root.findall("transaction")]
    cash_fact = next((f for f in draft["facts"] if f["key"] == "cash_total"), None)
    xml_cash = sum(float(t.findtext("amount_local") or 0) for t in root.findall("transaction") if t.findtext("transmode_code") == "K")
    if cash_fact and xml_cash:
        ok = xml_cash == cash_fact["value"]["total"]
        checks.append({"check": "cash transactions in XML sum to the narrative's cash total", "ok": ok,
                       "detail": f"XML PKR {xml_cash:,.0f} vs fact {cash_fact['id']} PKR {cash_fact['value']['total']:,.0f}"})
    for side, tag in (("credits_total", "t_to_my_client"), ("debits_total", "t_from_my_client")):
        fact = next((f for f in draft["facts"] if f["key"] == side), None)
        xml_sum = sum(float(t.findtext("amount_local") or 0) for t in root.findall("transaction") if t.find(tag) is not None)
        if fact:
            word = "credits" if side == "credits_total" else "debits"
            checks.append({"check": f"{word} in XML sum to the narrative's {word} total",
                           "ok": abs(xml_sum - fact["value"]["total"]) < 0.005,
                           "detail": f"XML PKR {xml_sum:,.0f} vs fact {fact['id']} PKR {fact['value']['total']:,.0f}"})
    checks.append({"check": "all amounts positive", "ok": all(a > 0 for a in amounts), "detail": f"{len(amounts)} transactions"})
    return {"valid": all(c["ok"] for c in checks), "checks": checks, "note": template["goaml"]["schema_note"]}
