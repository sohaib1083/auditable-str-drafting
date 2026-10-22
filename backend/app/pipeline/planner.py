"""Planner (agent 4): hypotheses + template J → outline, or abstain.

The outline is a list of slots. Each slot says what one sentence must
achieve and lists the only fact / hypothesis IDs that sentence may cite.
The allow-list is enforced in code by the verifier (a citation outside it
fails), so the drafter cannot pull in evidence the plan did not sanction —
this is also the prompt-injection boundary (P1).

If no hypothesis reaches the confidence threshold, the planner abstains:
the system reports "insufficient grounds" with the reasons, rather than
writing a persuasive narrative for a weak case (S3).
"""
from __future__ import annotations

from typing import Any

from app.pipeline.facts import FactTable


def _ids(ft: FactTable, *keys: str) -> list[str]:
    return [fid for k in keys for fid in ft.by_key.get(k, [])]


def plan(ft: FactTable, hyps: list[dict[str, Any]], template: dict[str, Any]) -> dict[str, Any]:
    supported = [h for h in hyps if h["supported"]]
    if not supported:
        best = max(hyps, key=lambda h: h["confidence"])
        return {"abstain": {
            "decision": "INSUFFICIENT_GROUNDS",
            "summary": ("No typology reached the confidence threshold after counter-evidence was weighed. "
                        "The system will not draft a narrative for this case."),
            "best_hypothesis": {"id": best["id"], "name": best["name"], "confidence": best["confidence"]},
            "hypotheses": [{"id": h["id"], "name": h["name"], "confidence": h["confidence"],
                            "support": [i["text"] for i in h["support"]],
                            "counter": [i["text"] for i in h["counter"]],
                            "counter_fact_ids": h["counter_fact_ids"]} for h in hyps],
            "missing": ["why: no supported typology with indicator evidence"]
                       + ([] if ft.by_key.get("declared_income") else
                          ["KYC profile (occupation, declared income): none on record, so activity could not be compared "
                           "with the customer's profile. Attach the KYC record to the case and re-run."])
                       + ([] if ft.by_key.get("screening") else ["Sanctions / PEP / adverse-media screening result"])
                       + ([] if any(k.startswith("note") for k in ft.by_key) else
                          ["Investigator findings (e.g. customer interview, recall requests from sender banks)"]),
            "recommendation": ("Record a documented no-file decision with reasons, or gather further evidence "
                               "(e.g. source-of-funds documents) and re-run. A human may still decide to file."),
        }}

    slots: list[dict[str, Any]] = []

    def slot(sid: str, section: str, element: str, intent: str, allowed: list[str]) -> None:
        if allowed:
            slots.append({"slot": sid, "section": section, "element": element, "intent": intent, "allowed": allowed})

    slot("subject.identity", "subject", "who", "Identify the subject, the account and when it was opened.",
         _ids(ft, "subject_name", "subject_account", "account_opened"))
    slot("subject.profile", "subject", "profile", "State the subject's declared occupation and declared income or turnover.",
         _ids(ft, "occupation", "declared_income", "declared_turnover"))
    slot("activity.totals", "activity", "what", "Summarise the activity window and total credits and debits.",
         _ids(ft, "activity_window", "credits_total", "debits_total"))
    if ft.by_key.get("cash_total"):
        slot("activity.cash", "activity", "where", "Describe the cash deposits and the branches used.",
             _ids(ft, "cash_total", "branches"))
    else:
        slot("activity.channels", "activity", "where", "Describe how the credits arrived and from how many senders.",
             _ids(ft, "channels_in", "distinct_senders"))
    slot("activity.detection", "activity", "when", "State when and how the activity was detected by Tazama.",
         _ids(ft, "detection_date", "tazama_typology"))

    for h in supported:
        for ind in h["support"]:
            slot(f"indicators.{h['id']}.{ind['id']}", "indicators", "why",
                 f"Explain the red flag: {ind['text']}.", ind["fact_ids"] + [h["id"]])
        slot(f"indicators.{h['id']}.summary", "indicators", "why",
             f"Conclude, with hedged language, that the activity is consistent with suspected {h['short']}.",
             [h["id"]] + h["support_fact_ids"][:4])

    for n, fid in enumerate(_ids(ft, "beneficiary")[:3], 1):
        slot(f"flow.beneficiary.{n}", "flow", "how", "State where the funds were sent.", [fid])
    ratio_ids = _ids(ft, "outflow_ratio", "pass_through_time")
    if not any(set(ratio_ids) <= set(s["allowed"]) for s in slots):  # already stated as a red flag
        slot("flow.ratio", "flow", "how", "State the share of credits paid out and how quickly.", ratio_ids)

    slot("counter.screening", "counter", "screening", "State the screening result.", _ids(ft, "screening"))
    for h in supported:
        for ind in h["counter"]:
            slot(f"counter.{h['id']}.{ind['id']}", "counter", "why", f"Note the counter-indicator: {ind['text']}.",
                 ind["fact_ids"] + [h["id"]])
    for fid in _ids(ft, "note_action"):
        slot(f"action.{fid}", "action", "action", "State the action the reporting entity has taken.", [fid])

    present = {s["element"] for s in slots}
    gaps = [e["id"] for e in template["required_elements"] if e["id"] not in present]
    return {"slots": slots, "supported": [h["id"] for h in supported], "gaps": gaps}
