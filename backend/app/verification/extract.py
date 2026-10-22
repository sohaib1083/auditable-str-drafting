"""Atom extraction: numbers, dates, codes and names from a piece of text.

The same extractor runs over a sentence and over the display strings of the
facts it cites. A sentence atom is supported only if it also appears among the
cited facts' atoms. That is the whole deterministic check, and it is why the
Fact Builder renders every value exactly once.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]
_M = "|".join(m.capitalize() for m in MONTHS)
_MS = "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"

DATE_DMY = re.compile(rf"\b(\d{{1,2}})\s+({_M}|{_MS})\.?,?\s+(\d{{4}})\b")
DATE_MDY = re.compile(rf"\b({_M}|{_MS})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})\b")
DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?:T[\d:.+\-]+)?\b")
DATE_MY = re.compile(rf"\b({_M})\s+(\d{{4}})\b")

CODE = re.compile(
    r"[A-Z]{2}\*\*[A-Z]{4}\*{4}\d{4}"      # masked IBAN-style account
    r"|WLT\*{4}\d{4}|\*{3}\d{4}"           # masked wallet
    r"|\b\d{3}@\d+\.\d+\.\d+\b"            # Tazama rule id
    r"|\b[A-Z]{2,5}(?:-[A-Z]{2})?-\d{2}(?:@\d+\.\d+\.\d+)?\b"  # typology cfg / indicator id
    r"|\bTX\d{4}-\d{4}\b|\bALRT-\d{4}-\d{2}\b|\[ENTITY_\d{2}\]")

NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(\s*(?:million|m\b))?(?![\w])", re.I)

WORD_NUMS = {w: n for n, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split())}
WORD_NUM_RE = re.compile(r"\b(" + "|".join(WORD_NUMS) + r")\b", re.I)


def _month(tok: str) -> int:
    t = tok.lower().rstrip(".")[:3]
    return [m[:3] for m in MONTHS].index(t) + 1


def dates(text: str) -> tuple[set[str], str]:
    """Return ISO dates (YYYY-MM-DD, or YYYY-MM for month-only) and the text with dates removed."""
    found: set[str] = set()

    def dmy(m: re.Match[str]) -> str:
        found.add(f"{int(m.group(3)):04d}-{_month(m.group(2)):02d}-{int(m.group(1)):02d}")
        return " "

    def mdy(m: re.Match[str]) -> str:
        found.add(f"{int(m.group(3)):04d}-{_month(m.group(1)):02d}-{int(m.group(2)):02d}")
        return " "

    def iso(m: re.Match[str]) -> str:
        found.add(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
        return " "

    def my(m: re.Match[str]) -> str:
        found.add(f"{int(m.group(2)):04d}-{_month(m.group(1)):02d}")
        return " "

    text = DATE_ISO.sub(iso, text)
    text = DATE_DMY.sub(dmy, text)
    text = DATE_MDY.sub(mdy, text)
    text = DATE_MY.sub(my, text)
    return found, text


def codes(text: str) -> tuple[set[str], str]:
    found = set(CODE.findall(text))
    return found, CODE.sub(" ", text)


def numbers(text: str) -> set[str]:
    out: set[str] = set()
    for m in NUMBER.finditer(text):
        try:
            v = Decimal(m.group(1).replace(",", "") + ("." + m.group(2) if m.group(2) else ""))
        except InvalidOperation:
            continue
        if m.group(3):
            v *= 1_000_000
        out.add(format(v.normalize(), "f"))
    for m in WORD_NUM_RE.finditer(text):
        out.add(str(WORD_NUMS[m.group(1).lower()]))
    return out


def atoms(text: str) -> dict[str, set[str]]:
    d, rest = dates(text)
    c, rest = codes(rest)
    return {"dates": d, "codes": c, "numbers": numbers(rest)}


# Capitalised words that are never case entities.
ALLOW = {
    "the", "a", "an", "this", "these", "those", "that", "it", "its", "of", "as", "at", "in", "on", "for", "from",
    "to", "by", "and", "or", "with", "during", "between", "taken", "together", "funds", "screening", "action",
    "taken:", "credits", "debits", "customer", "subject", "account", "measured", "against", "note", "however",
    "there", "no", "not", "all", "each", "both", "over", "under", "after", "before", "further", "also", "while",
    "including", "total", "within", "overall", "further,", "first", "second", "third", "rule", "rules", "cash",
    "branch", "branches", "tazama", "typology", "pkr", "str", "ctr", "sar", "fmu", "fiu", "ibft", "rtgs", "atm",
    "pep", "kyc", "aml", "cft", "edd", "cdd", "pakistan", "financial", "monitoring", "unit", "ramadan", "eid",
    "reporting", "entity", "report", "alert", "outgoing", "incoming", "inbound", "outbound", "since", "until",
    "when", "where", "which", "who", "what", "why", "how", "our", "we", "he", "she", "they", "his", "her", "their",
    "is", "was", "were", "has", "had", "have", "be", "been", "one", "two", "several", "many", "most", "some",
    "account:", "possible", "alternative", "explanation", "declared", "occupation", "income", "turnover",
    "business", "activity", "period", "wallet", "transfers", "deposits", "deposit", "withdrawals", "debit", "credit",
    "internal", "list", "synthetic", "l2", "analyst", "operations", "desk", "fraud", "manager", "relationship",
    "enhanced", "review", "suspected", "consistent", "indicative", "suspicious", "transaction", "transactions",
    "state", "bank", "main", "if", "any", "such", "other", "more", "than", "less", "only", "same", "day", "days",
}


NAME = re.compile(r"\b[A-Z][A-Za-z&'\-]*(?:\s+(?:[A-Z][A-Za-z&'\-]*|&|\(Pvt\)|of|e|and)){0,5}")


def names(text: str) -> set[str]:
    """Candidate proper names: capitalised runs with allow-listed words stripped from the edges."""
    _, rest = dates(text)
    _, rest = codes(rest)
    rest = re.sub(r'"[^"]*"', " ", rest)  # quoted evidence is checked against the note it quotes, not here
    out: set[str] = set()
    for m in NAME.finditer(rest):
        words = m.group(0).split()
        while words and words[0].lower().strip(",:;()") in ALLOW:
            words.pop(0)
        while words and (words[-1].lower().strip(",:;()") in ALLOW or words[-1] in ("of", "and", "e", "&")):
            words.pop()
        if words and any(w[:1].isupper() for w in words):
            cand = " ".join(words).strip(",:;()")
            if len(cand) > 2 and cand.lower() not in {x for x in MONTHS}:
                out.add(cand)
    return out
