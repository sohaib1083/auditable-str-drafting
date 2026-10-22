"""Fact Builder (agent 2): Evidence → Fact Table F with lineage.

"The model writes the words; tools compute the numbers." Every amount, count,
date and ratio a narrative may state is computed here by code, carries the
evidence IDs it was derived from, and is rendered once into a canonical
display string. The drafter may only restate these strings; the deterministic
verifier checks that it did.

Methods:
  DETERMINISTIC  computed by a calc.* tool from evidence
  EXTRACTED      copied from a structured field (KYC, screening, rule result)
  HUMAN          an investigator or branch note, quoted not interpreted
  REFERENCE      jurisdiction reference data (e.g. the CTR threshold)
"""
from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from app import chain

TOOL_VERSION = "1.0.0"

CHANNEL_NAMES = {"CASH_DEPOSIT": "cash deposits", "IBFT": "interbank (IBFT) transfers", "RTGS": "RTGS transfers",
                 "WALLET_TRANSFER": "wallet transfers", "ATM_WITHDRAWAL": "ATM withdrawals"}


# ── Rendering: one canonical string per value ────────────────────────────────

def pkr(n: float | int) -> str:
    return f"PKR {int(round(n)):,}"


def parse_ts(iso: str) -> datetime:
    """ISO 8601 including the trailing 'Z' that Tazama and the CMS emit."""
    return datetime.fromisoformat(iso[:-1] + "+00:00" if iso.endswith("Z") else iso)


REPORT_TZ = timezone(timedelta(hours=5), "PKT")  # dates in a goAML-PK report are Pakistan dates


def day(iso: str) -> str:
    d = parse_ts(iso)
    if d.tzinfo:
        d = d.astimezone(REPORT_TZ)
    return f"{d.day} {d.strftime('%B %Y')}"


def pct(x: float) -> str:
    return f"{round(x * 100)}%"


class FactTable:
    def __init__(self) -> None:
        self.facts: list[dict[str, Any]] = []
        self.by_key: dict[str, list[str]] = defaultdict(list)
        self.metrics: dict[str, Any] = {}

    def add(self, key: str, label: str, ftype: str, value: Any, display: str, derived_from: list[str],
            method: str, tool: str, entities: list[str] | None = None) -> str:
        fid = f"F{len(self.facts) + 1:02d}"
        fact = {"id": fid, "key": key, "label": label, "type": ftype, "value": value, "display": display,
                "entities": [e for e in (entities or []) if e], "derived_from": sorted(set(derived_from)),
                "method": method, "tool": tool, "tool_version": TOOL_VERSION}
        fact["hash"] = chain.content_hash({k: v for k, v in fact.items() if k != "id"})
        self.facts.append(fact)
        self.by_key[key].append(fid)
        return fid


def build_facts(evidence: list[dict[str, Any]]) -> FactTable:
    ft = FactTable()
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in evidence:
        by_type[e["type"]].append(e)

    kyc_ev = by_type["kyc"][0]
    kyc = kyc_ev["content"]
    ref_ev = by_type["reference"][0]
    ref = ref_ev["content"]
    alert_ev = by_type["tazama_alert"][0]
    alert = alert_ev["content"]
    txns = [(e["id"], e["content"]) for e in by_type["transaction"]]
    credits = [(i, t) for i, t in txns if t["direction"] == "in"]
    debits = [(i, t) for i, t in txns if t["direction"] == "out"]
    cash = [(i, t) for i, t in credits if t["channel"] == "CASH_DEPOSIT"]
    m = ft.metrics

    # Subject (EXTRACTED from KYC)
    k = kyc_ev["id"]
    ft.add("subject_name", "Subject name", "entity", kyc["name"], kyc["name"], [k], "EXTRACTED", "kyc.field:name", [kyc["name"]])
    ft.add("subject_account", "Subject account", "entity", kyc["account"], kyc["account"], [k], "EXTRACTED", "kyc.field:account", [kyc["account"]])
    # Profile fields are stated only when a KYC record supplied them. A source
    # such as Tazama, which holds payments but no KYC, leaves them absent and the
    # profile indicators then cannot fire; nothing is filled in by assumption.
    if kyc.get("occupation"):
        ft.add("occupation", "Declared occupation", "text", kyc["occupation"], kyc["occupation"], [k], "EXTRACTED",
               "kyc.field:occupation", [kyc["employer"]] if kyc.get("employer") else [])
    m["is_student"] = "student" in (kyc.get("occupation") or "").lower()
    if kyc.get("account_opened"):
        ft.add("account_opened", "Account opened", "date", kyc["account_opened"], day(kyc["account_opened"]), [k],
               "EXTRACTED", "kyc.field:account_opened")
    if kyc.get("declared_monthly_income"):
        ft.add("declared_income", "Declared monthly income", "amount", kyc["declared_monthly_income"],
               f"{pkr(kyc['declared_monthly_income'])} per month", [k], "EXTRACTED", "kyc.field:declared_monthly_income")
    if kyc.get("declared_monthly_turnover"):
        ft.add("declared_turnover", "Declared monthly business turnover", "amount", kyc["declared_monthly_turnover"],
               f"{pkr(kyc['declared_monthly_turnover'])} per month", [k], "EXTRACTED", "kyc.field:declared_monthly_turnover")

    # Detection (EXTRACTED from the Tazama alert)
    a = alert_ev["id"]
    typ_id = alert["typology"].split("@")[0]
    ft.add("detection_date", "Alert raised by Tazama", "date", alert["timestamp"][:10], day(alert["timestamp"]), [a], "EXTRACTED", "tazama.alert:timestamp")
    ft.add("tazama_typology", "Tazama typology result", "score", {"typology": alert["typology"], "score": alert["score"], "threshold": alert["alertThreshold"]},
           f"Tazama typology {typ_id} scored {alert['score']} against an alert threshold of {alert['alertThreshold']}",
           [a], "EXTRACTED", "tazama.alert:typologyResult")
    m["tazama_typology"] = typ_id
    for r in by_type["rule_result"]:
        rr = r["content"]
        ft.add("rule_result", f"Tazama rule {rr['id']}", "text", rr["reason"], f"Tazama rule {rr['id']}: {rr['reason']}",
               [r["id"]], "EXTRACTED", "tazama.rule:reason")

    # Activity totals (DETERMINISTIC)
    all_ids = [i for i, _ in txns]
    dates = sorted(t["booked_at"] for _, t in txns)
    ft.add("activity_window", "Activity window", "date_range", {"start": dates[0][:10], "end": dates[-1][:10]},
           f"{day(dates[0])} to {day(dates[-1])}", all_ids, "DETERMINISTIC", "calc.window")
    c_total = sum(t["amount"] for _, t in credits)
    d_total = sum(t["amount"] for _, t in debits)
    ft.add("credits_total", "Total credits", "amount", {"count": len(credits), "total": c_total},
           f"{len(credits)} credits totalling {pkr(c_total)}", [i for i, _ in credits], "DETERMINISTIC", "calc.sum")
    ft.add("debits_total", "Total debits", "amount", {"count": len(debits), "total": d_total},
           f"{len(debits)} debits totalling {pkr(d_total)}", [i for i, _ in debits], "DETERMINISTIC", "calc.sum")
    m.update(credits_total=c_total, debits_total=d_total)

    channels = Counter(t["channel"] for _, t in credits)
    ft.add("channels_in", "Credit channels", "text", dict(channels),
           ", ".join(f"{n} {CHANNEL_NAMES.get(ch, ch.lower())}" for ch, n in channels.most_common()),
           [i for i, _ in credits], "DETERMINISTIC", "calc.group_by_channel")

    # Cash-specific facts
    threshold = ref["ctr_threshold"]
    low = threshold * ref["near_threshold_band_pct"] / 100
    ft.add("ctr_threshold", "Cash transaction reporting threshold", "amount", threshold, pkr(threshold),
           [ref_ev["id"]], "REFERENCE", "template.reference")
    m["near_threshold_count"] = m["near_threshold_share"] = m["above_threshold_count"] = 0
    m["branch_count"] = m["same_day_days"] = 0
    if cash:
        cash_total = sum(t["amount"] for _, t in cash)
        ft.add("cash_total", "Cash deposits", "amount", {"count": len(cash), "total": cash_total},
               f"{len(cash)} cash deposits totalling {pkr(cash_total)}", [i for i, _ in cash], "DETERMINISTIC", "calc.sum")
        near = [(i, t) for i, t in cash if low <= t["amount"] < threshold]
        above = [(i, t) for i, t in cash if t["amount"] >= threshold]
        share = len(near) / len(cash)
        m.update(near_threshold_count=len(near), near_threshold_share=share, above_threshold_count=len(above))
        near_total = sum(t["amount"] for _, t in near)
        ft.add("near_threshold", "Cash deposits just below the threshold", "amount",
               {"count": len(near), "of": len(cash), "total": near_total, "band": [low, threshold - 1]},
               f"{len(near)} of {len(cash)} cash deposits ({pct(share)}) were between {pkr(low)} and {pkr(threshold - 1)}, totalling {pkr(near_total)}",
               [i for i, _ in near] + [ref_ev["id"]], "DETERMINISTIC", "calc.threshold_band")
        if above:
            ft.add("above_threshold", "Cash deposits at or above the threshold", "amount",
                   {"count": len(above), "total": sum(t["amount"] for _, t in above)},
                   f"{len(above)} cash deposits of {pkr(threshold)} or more, totalling {pkr(sum(t['amount'] for _, t in above))}",
                   [i for i, _ in above] + [ref_ev["id"]], "DETERMINISTIC", "calc.threshold_band")
        br = Counter(t["branch"] for _, t in cash)
        m["branch_count"] = len(br)
        names = sorted(br)
        ft.add("branches", "Branches used for cash deposits", "entity_list", dict(br),
               f"{len(br)} {'branch' if len(br) == 1 else 'branches'} ({', '.join(names)})",
               [i for i, _ in cash], "DETERMINISTIC", "calc.distinct", names + sorted({t["city"] for _, t in cash}))
        per_day = defaultdict(list)
        for i, t in cash:
            per_day[t["booked_at"][:10]].append(i)
        multi = {d: ids for d, ids in per_day.items() if len(ids) > 1}
        m["same_day_days"] = len(multi)
        if multi:
            ds = sorted(multi)
            ft.add("same_day_cash", "Days with more than one cash deposit", "date_list", ds,
                   f"more than one cash deposit on {len(ds)} {'day' if len(ds) == 1 else 'days'} ({', '.join(day(d) for d in ds)})",
                   [i for d in ds for i in multi[d]], "DETERMINISTIC", "calc.same_day")

    # Profile comparison
    inc = kyc.get("declared_monthly_income")
    multiple = c_total / inc if inc else 0
    m["income_multiple"] = round(multiple, 1)
    if inc:
        ft.add("income_multiple", "Credits compared with declared monthly income", "ratio", round(multiple, 1),
               f"credits in the activity window were {multiple:.1f} times the declared monthly income",
               [i for i, _ in credits] + [k], "DETERMINISTIC", "calc.ratio")
    m["turnover_ratio"] = 0
    if kyc.get("declared_monthly_turnover"):
        tr = c_total / kyc["declared_monthly_turnover"]
        m["turnover_ratio"] = round(tr, 2)
        ft.add("turnover_ratio", "Credits compared with declared turnover", "ratio", round(tr, 2),
               f"credits in the activity window were {pct(tr)} of the declared monthly business turnover",
               [i for i, _ in credits] + [k], "DETERMINISTIC", "calc.ratio")
    m["turnover_ratio_or_zero"] = m["turnover_ratio"]

    # Counterparties
    senders = {t["counterparty_name"] for _, t in credits if t["channel"] != "CASH_DEPOSIT"}
    m["distinct_senders"] = len(senders)
    if len(senders) >= 2:
        ft.add("distinct_senders", "Distinct inbound senders", "count", len(senders),
               f"{len(senders)} distinct senders", [i for i, t in credits if t["channel"] != "CASH_DEPOSIT"],
               "DETERMINISTIC", "calc.distinct")

    groups: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for i, t in debits:
        groups[t["counterparty_name"]].append((i, t))
    for name, items in sorted(groups.items(), key=lambda g: -sum(t["amount"] for _, t in g[1])):
        t0 = items[0][1]
        total = sum(t["amount"] for _, t in items)
        acct = f" ({t0['counterparty_account']}, {t0['counterparty_bank']})" if t0["counterparty_account"] else ""
        verb = "was withdrawn as" if t0["channel"] == "ATM_WITHDRAWAL" else "was sent to"
        ft.add("beneficiary", f"Outflow to {name}", "amount", {"name": name, "count": len(items), "total": total},
               f"{pkr(total)} {verb} {name}{acct} in {len(items)} {'transaction' if len(items) == 1 else 'transactions'}",
               [i for i, _ in items], "DETERMINISTIC", "calc.group_by_counterparty",
               [name, t0["counterparty_account"], t0["counterparty_bank"]])
    atm = [(i, t) for i, t in debits if t["channel"] == "ATM_WITHDRAWAL"]
    if atm:
        locs = sorted({f"{t['branch']}, {t['city']}" for _, t in atm})
        ft.add("atm_withdrawals", "ATM cash withdrawals", "amount", {"count": len(atm), "total": sum(t["amount"] for _, t in atm)},
               f"{len(atm)} ATM cash withdrawals at {', '.join(locs)}", [i for i, _ in atm], "DETERMINISTIC", "calc.sum",
               [x for _, t in atm for x in (t["branch"], t["city"])])

    ratio = d_total / c_total if c_total else 0
    m["outflow_ratio"] = round(ratio, 2)
    ft.add("outflow_ratio", "Share of credits paid out", "ratio", round(ratio, 2),
           f"debits equal to {pct(ratio)} of credits", all_ids, "DETERMINISTIC", "calc.ratio")

    gaps = []
    ordered = sorted(txns, key=lambda x: x[1]["booked_at"])
    last_credit: datetime | None = None
    for _, t in ordered:
        at = parse_ts(t["booked_at"])
        if t["direction"] == "in":
            last_credit = at
        elif last_credit is not None:
            gaps.append((at - last_credit).total_seconds() / 3600)
    m["median_hours_to_outflow"] = round(statistics.median(gaps), 1) if gaps else 9999
    if gaps:
        ft.add("pass_through_time", "Time from last credit to each debit", "duration", m["median_hours_to_outflow"],
               f"a median of {statistics.median(gaps):.1f} hours between the latest credit and each debit",
               [i for i, _ in ordered], "DETERMINISTIC", "calc.pass_through")

    # Screening (EXTRACTED)
    for s_ev in by_type["screening"][:1]:
        s = s_ev["content"]
        hits = [n for n, v in (("sanctions", s["sanctions"]), ("PEP", s["pep"]), ("adverse-media", s["adverse_media"])) if v != "NO_MATCH"]
        ft.add("screening", "Screening result", "text", {k2: s[k2] for k2 in ("sanctions", "pep", "adverse_media")},
               (f"no match on sanctions, PEP or adverse-media screening on {day(s['screened_at'])}" if not hits
                else f"{', '.join(hits)} screening returned a match on {day(s['screened_at'])}"),
               [s_ev["id"]], "EXTRACTED", "screening.result")

    # Investigator and branch notes (HUMAN): quoted, then tagged by simple rules
    m.update(note_source_of_funds=False, note_seasonal=False, note_recall=False, note_supplier_docs=False)
    for e in by_type["case_note"]:
        n = e["content"]
        text = n["text"]
        low_t = text.lower()
        key = "note"
        if re.search(r"declined to explain|source of (the )?(cash|funds)", low_t):
            key, m["note_source_of_funds"] = "note_source_of_funds", True
        elif re.search(r"recall", low_t):
            key, m["note_recall"] = "note_recall", True
        elif re.search(r"ramadan|seasonal|every year", low_t):
            key, m["note_seasonal"] = "note_seasonal", True
            m["note_supplier_docs"] = "invoice" in low_t
        elif re.search(r"placed (under|on)|hold|enhanced monitoring|ctrs were filed", low_t):
            key = "note_action"
        ft.add(key, f"Note by {n['author']}", "note", text, f"{n['author']} ({day(n['at'])}): \"{text}\"",
               [e["id"]], "HUMAN", "cms.notes", [n["author"]])

    m["tazama_match"] = True
    return ft
