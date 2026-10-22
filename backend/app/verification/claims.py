"""Claim-level verification stack (layers 6a, 6b-lite and 6f).

Each sentence is treated as one claim in the MVP. Checks return
pass / warn / fail. A fail is release-blocking: the orchestrator sends the
sentence back to the drafter, then removes it if repair does not fix it.
A warn reaches the human reviewer as a flag.

  A1  citation present and resolvable          (lineage)
  P1  citations inside the planner's allow-list (injection boundary)
  H1  numbers appear in the cited facts         (deterministic)
  H2  dates appear in the cited facts           (deterministic)
  H3  codes, accounts and names appear in cited facts; no novel entities
  H4  no statement of intent or guilt; hedged typology language
  H5  typology asserted only with a supported hypothesis
  S1  no tipping-off language
  S2  no national IDs, phone numbers, unmasked accounts, protected characteristics
  SUP each citation is actually used by the sentence (lexical support, warn only)
"""
from __future__ import annotations

import re
from typing import Any

from app.verification import semantic

from app.verification import extract

VERIFIER_VERSION = "verifier-1.0.0"

TYPOLOGY_TERMS = ["structuring", "smurfing", "money mule", "mule", "pass-through", "layering", "money laundering",
                  "terrorist financing", "threshold avoidance", "unexplained increase"]
STOP = {"which", "their", "there", "about", "after", "before", "these", "those", "within", "between", "account",
        "customer", "subject", "during", "period", "total", "totalling", "against", "reporting", "entity"}

PII_PATTERNS = [
    (re.compile(r"\b\d{5}-\d{7}-\d\b"), "national ID (CNIC) number"),
    (re.compile(r"(?:\+92|\b0)3\d{2}[- ]?\d{7}\b"), "phone number"),
    (re.compile(r"\b\d{10,}\b"), "unmasked account or identifier number"),
    (re.compile(r"\b[\w.+-]+@[A-Za-z][\w-]*\.[A-Za-z]{2,}\b"), "email address"),
]


def _check(code: str, status: str, message: str) -> dict[str, str]:
    return {"code": code, "status": status, "message": message}


def build_lexicon(facts: list[dict[str, Any]]) -> set[str]:
    return {e for f in facts for e in f.get("entities", []) if e and len(e) > 3}


def verify_sentence(sent: dict[str, Any], facts: dict[str, dict[str, Any]], hyps: dict[str, dict[str, Any]],
                    allowed: list[str] | None, lexicon: set[str], style: dict[str, Any]) -> dict[str, Any]:
    text = sent["text"]
    low = text.lower()
    cites = sent.get("cites") or []
    checks: list[dict[str, str]] = []

    # A1 lineage
    if not cites:
        checks.append(_check("A1", "fail", "Sentence has no citation, so it cannot be traced to evidence."))
    unresolved = [c for c in cites if c not in facts and c not in hyps]
    if unresolved:
        checks.append(_check("A1", "fail", f"Citation(s) {', '.join(unresolved)} do not exist in this case's fact table."))
    if cites and not unresolved:
        checks.append(_check("A1", "pass", "All citations resolve to facts or hypotheses in this case."))

    # P1 plan allow-list
    if allowed is None:
        checks.append(_check("P1", "fail", "Sentence does not belong to any slot the planner requested."))
    else:
        outside = [c for c in cites if c not in allowed and c not in unresolved]
        if outside:
            checks.append(_check("P1", "fail", f"Citation(s) {', '.join(outside)} are outside the planner's allow-list for this slot."))
        else:
            checks.append(_check("P1", "pass", "Citations are within the planner's allow-list."))

    cited_facts = [facts[c] for c in cites if c in facts]
    cited_hyps = [hyps[c] for c in cites if c in hyps]
    src_text = " ".join(f["display"] for f in cited_facts)
    src = extract.atoms(src_text)
    for f in cited_facts:
        if f["type"] == "date_range":
            src["dates"].update({f["value"]["start"], f["value"]["end"]})
    s_atoms = extract.atoms(text)

    # H1 numbers
    bad_nums = sorted(s_atoms["numbers"] - src["numbers"])
    if bad_nums:
        checks.append(_check("H1", "fail", f"Number(s) {', '.join(bad_nums)} do not appear in any cited fact."))
    elif s_atoms["numbers"]:
        checks.append(_check("H1", "pass", f"{len(s_atoms['numbers'])} number(s) match the cited facts exactly."))

    # H2 dates (a month-only mention is supported by any cited date in that month)
    src_months = {d[:7] for d in src["dates"]}
    bad_dates = sorted(d for d in s_atoms["dates"] if d not in src["dates"] and not (len(d) == 7 and d in src_months))
    if bad_dates:
        checks.append(_check("H2", "fail", f"Date(s) {', '.join(bad_dates)} do not appear in any cited fact."))
    elif s_atoms["dates"]:
        checks.append(_check("H2", "pass", f"{len(s_atoms['dates'])} date(s) match the cited facts."))

    # H3 codes / accounts / names
    bad_codes = sorted(c for c in s_atoms["codes"] if c not in src_text)
    if bad_codes:
        checks.append(_check("H3", "fail", f"Identifier(s) {', '.join(bad_codes)} do not appear in any cited fact."))
    cited_ents = {e for f in cited_facts for e in f.get("entities", [])}
    misattributed = sorted(e for e in lexicon if e in text and e not in src_text and e not in cited_ents)
    if misattributed:
        checks.append(_check("H3", "fail", f"{', '.join(misattributed)} appear(s) in the case but not in the cited facts."))
    novel = []
    lex_low = [x.lower() for x in lexicon] + [src_text.lower()]
    for cand in extract.names(text):
        c = cand.lower()
        if any(c in x for x in lex_low):
            continue
        novel.append(cand)
    if novel:
        multi = [n for n in novel if " " in n or re.search(r"\b(Ltd|FZE|LLC|Inc|Bank|Company|Holdings|Traders)\b", n)]
        status = "fail" if multi else "warn"
        checks.append(_check("H3", status, f"Name(s) not found anywhere in the case evidence: {', '.join(sorted(novel))}."))
    if not bad_codes and not misattributed and not novel and (s_atoms["codes"] or any(e in text for e in lexicon)):
        checks.append(_check("H3", "pass", "All names and identifiers come from the cited facts."))

    # H4 intent / hedging, H5 typology support
    intent = [w for w in style["forbidden_intent"] if re.search(rf"\b{re.escape(w)}\b", low)]
    quoted = " ".join(re.findall(r'"([^"]*)"', text)).lower()
    intent = [w for w in intent if w not in quoted]
    if intent:
        checks.append(_check("H4", "fail", f"States intent or guilt ({', '.join(intent)}). Use hedged language; the FIU decides."))
    low_nocode = extract.codes(text)[1].lower()
    typ_terms = [t for t in TYPOLOGY_TERMS if t in low_nocode and t not in quoted]
    if typ_terms:
        if not any(h["supported"] for h in cited_hyps):
            checks.append(_check("H5", "fail", f"Asserts a typology ({typ_terms[0]}) without citing a supported hypothesis."))
        else:
            checks.append(_check("H5", "pass", "Typology language is backed by a supported hypothesis."))
        if not any(h in low for h in style["hedges"]):
            checks.append(_check("H4", "fail", "Typology stated without hedging (e.g. 'consistent with', 'suspected')."))
        elif not intent:
            checks.append(_check("H4", "pass", "Suspicion is expressed with hedged language."))

    # S1 tipping-off
    tip = [p for p in style["tipping_off"] if p in low]
    if tip:
        checks.append(_check("S1", "fail", f"Possible tipping-off language: \"{tip[0]}\"."))

    # S2 PII and protected characteristics
    for rx, what in PII_PATTERNS:
        if rx.search(text):
            checks.append(_check("S2", "fail", f"Contains a {what}; not required for this report."))
    prot = [w for w in style["protected_characteristics"] if re.search(rf"\b{w}\b", low)]
    if prot:
        checks.append(_check("S2", "fail", f"Mentions a protected characteristic ({prot[0]})."))

    # SUP lexical support: every cited fact should visibly contribute
    s_words = {w for w in re.findall(r"[a-z]{5,}", low) if w not in STOP}
    for f in cited_facts:
        fa = extract.atoms(f["display"])
        if f["type"] == "date_range":
            fa["dates"].update({f["value"]["start"], f["value"]["end"]})
        used = (fa["numbers"] & s_atoms["numbers"]) or (fa["dates"] & s_atoms["dates"]) or (fa["codes"] & s_atoms["codes"]) \
            or any(e in text for e in f.get("entities", []))
        if not used:
            f_words = {w for w in re.findall(r"[a-z]{5,}", (f["display"] + " " + f["label"]).lower()) if w not in STOP}
            used = len(s_words & f_words) >= 2
        if not used:
            checks.append(_check("SUP", "warn", f"{f['id']} ({f['label']}) is cited but does not appear to support this sentence."))

    # SEM semantic support: optional third layer, only for sentences the deterministic layers passed
    if semantic.enabled() and not any(c["status"] == "fail" for c in checks):
        evidence = [f"{facts[c].get('label', 'Fact')}: {facts[c].get('display', '')}" if c in facts else
                    f"Typology hypothesis: {hyps[c].get('display', '')}" for c in cites if c in facts or c in hyps]
        try:
            res = semantic.check(text, [e for e in evidence if e])
            checks.append(_check("SEM", "pass", "Cited evidence supports every statement.") if res["supported"] else
                          _check("SEM", "fail", f"Not supported by the cited evidence: {res['unsupported_part'][:160]}"))
        except RuntimeError as e:
            checks.append(_check("SEM", "warn", f"Semantic check unavailable ({str(e)[:80]}); needs human review."))

    status = "failed" if any(c["status"] == "fail" for c in checks) else \
        "flagged" if any(c["status"] == "warn" for c in checks) else "supported"
    return {"status": status, "checks": checks, "verifier": VERIFIER_VERSION,
            "atoms": {k: sorted(v) for k, v in s_atoms.items()}}


# Instruction-like text inside evidence: imperatives aimed at the drafter, role or mode
# switches, and attempts to dictate the report's content or outcome.
INJECTION = re.compile(
    r"\b(ignore|disregard|forget|override)\b.{0,40}\b(instructions?|evidence|above|previous|prior|rules?|data)\b"
    r"|system prompt|^\s*(system|assistant)\s*:|\b(admin|developer|debug|god) mode\b"
    r"|\byou are (now )?(an? )?(ai|assistant|language model|chatbot|in)\b"
    r"|\b(note|message|instruction)s? (to|for) (the )?(ai|model|assistant|llm|system|drafter)\b"
    r"|\b(ai|model|assistant|llm)\b.{0,30}\b(must|should|shall)\b"
    r"|\b(write|say|state|report|claim|conclude|mark|describe)\b.{0,25}\b(that|as)\b.{0,60}\b(innocent|low risk|legitimate|no report|not suspicious|inheritance|below)\b"
    r"|\bdo not (report|file|mention)\b|\bno (report|str|sar) is (needed|required)\b"
    r"|\b(delete|remove|omit)\b.{0,20}\b(figures?|amounts?|transactions?|evidence)\b",
    re.I | re.M)


def scan_evidence_for_injection(evidence: list[dict[str, Any]]) -> list[dict[str, str]]:
    """P1: find instruction-like text inside evidence. It is reported, never obeyed."""
    hits = []
    for e in evidence:
        for k, v in (e["content"].items() if isinstance(e["content"], dict) else []):
            if isinstance(v, str) and INJECTION.search(v):
                hits.append({"evidence_id": e["id"], "field": k, "excerpt": v[:140]})
    return hits
