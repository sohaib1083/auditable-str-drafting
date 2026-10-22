"""Evaluation harness: runs SAR Studio's real pipeline over SAR-Bench-lite and scores it.

Every experiment calls the production code (intake → facts → typology →
planner → drafter → verifier → integrity) on a throw-away SQLite database.
Scoring uses the benchmark's independent ground truth and plain string
checks against the raw source data, never SAR Studio's own fact table.
"""
from __future__ import annotations

import copy
import json
import os
import random
import re
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

EVAL = Path(__file__).resolve().parent
BACKEND = EVAL.parent / "backend"
_DB = Path(tempfile.mkdtemp(prefix="sarbench-")) / "bench.db"
os.environ["SAR_DB_PATH"] = str(_DB)
os.environ["SAR_STAGE_DELAY"] = "0"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(EVAL))

from app import audit, chain, db  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import dumps  # noqa: E402
from app.intake import ingest_case  # noqa: E402
from app.ledger import list_evidence  # noqa: E402
from app.pipeline import drafter as drafter_mod, gateway, orchestrator  # noqa: E402
from app.pipeline.facts import build_facts  # noqa: E402
from app.pipeline.planner import plan  # noqa: E402
from app.pipeline.typology import analyse  # noqa: E402
from app.templates import load_template  # noqa: E402
from app.verification import verify_sentences  # noqa: E402

TEMPLATE = load_template("GOAML:PK")
db.init_db()


# ── Running the real pipeline ────────────────────────────────────────────────

def ingest(bundle: dict[str, Any]) -> str:
    with db.session() as conn:
        if conn.execute("SELECT 1 FROM cases WHERE id=?", (bundle["case"]["id"],)).fetchone():
            return bundle["case"]["id"]
        return ingest_case(conn, bundle, "bench", actor="bench:intake")


def draft(case_id: str, provider: str = "offline", inject_faults: bool = False) -> dict[str, Any]:
    with db.session() as conn:
        v = (conn.execute("SELECT MAX(version) FROM drafts WHERE case_id=?", (case_id,)).fetchone()[0] or 0) + 1
        did = f"D{case_id.split('-')[-1]}-{v}-{uuid.uuid4().hex[:4]}"
        now = chain.now_iso()
        conn.execute("INSERT INTO drafts (id, case_id, version, status, jurisdiction, template_version, provider, endpoint_json,"
                     " inject_faults, requested_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (did, case_id, v, "REQUESTED", "GOAML:PK", "goaml-pk-str@1.0.0", provider,
                      dumps(gateway.ENDPOINTS[provider].public()), int(inject_faults), "bench", now, now))
    orchestrator.run(did)
    conn = db.connect()
    try:
        d = db.row(conn.execute("SELECT * FROM drafts WHERE id=?", (did,)).fetchone())
        sents = db.rows(conn.execute("SELECT * FROM sentences WHERE draft_id=? ORDER BY idx", (did,)).fetchall())
        ev = list_evidence(conn, case_id)
    finally:
        conn.close()
    return {"draft": d, "sentences": sents, "evidence": ev}


def outline_for(case_id: str) -> tuple[list, list, dict, list]:
    """Facts, hypotheses, outline and the drafter's raw (pre-verification) sentences."""
    conn = db.connect()
    try:
        ev = list_evidence(conn, case_id)
    finally:
        conn.close()
    ft = build_facts(ev)
    hyps = analyse(ft, settings.abstain_confidence)
    out = plan(ft, hyps, TEMPLATE)
    raw = drafter_mod.OfflineDrafter(ft.facts, hyps).draft(out) if "slots" in out else []
    return ft.facts, hyps, out, raw


# ── Independent scoring helpers (no SAR Studio code) ─────────────────────────

PKR = re.compile(r"PKR ([\d,]+(?:\.\d+)?)")
PKR_M = re.compile(r"PKR ([\d,]+(?:\.\d+)?)( million)?")
CAP_PHRASE = re.compile(r"\b(?:[A-Z][a-zA-Z&'.-]+)(?:\s+(?:[A-Z][a-zA-Z&'.-]+|of|and|&))*\s+[A-Z][a-zA-Z&'.-]+\b")
GUILT = re.compile(r"\b(knowingly|deliberately laundered|is a (?:money launderer|criminal)|criminal proceeds|proves that|"
                   r"guilty|committed (?:fraud|money laundering))\b", re.I)
TIPOFF = re.compile(r"\b(customer|subject) (?:has been|was|will be) (?:informed|told|notified|advised)\b|"
                    r"\binformed (?:the customer|him|her) (?:of|about|that)\b", re.I)
PII = re.compile(r"\b\d{5}-\d{7}-\d\b|(?:\+92|\b0)3\d{2}[- ]?\d{7}\b")
ALWAYS_OK = {"Tazama", "PKR", "Synthetic", "Branch Operations", "Fraud Desk", "Relationship Manager"}


def source_text(bundle: dict[str, Any]) -> str:
    b = {k: v for k, v in bundle.items() if not k.startswith("_")}
    return json.dumps(b, ensure_ascii=False, default=str)


def amount_universe(truth: dict[str, Any]) -> set[float]:
    u = set(truth["amounts"]) | {truth["credits_total"], truth["debits_total"], truth["cash_total"], truth["near_total"],
                                 2_000_000, 1_600_000, 1_999_999}
    u |= {v[0] for v in truth["beneficiaries"].values()}
    if truth["income"]:
        u.add(truth["income"])
    return {float(x) for x in u}


def novel_entities(text: str, src: str) -> list[str]:
    out = []
    for m in CAP_PHRASE.finditer(text):
        p = m.group(0)
        words = p.split()
        if p in ALWAYS_OK or any(w in {"January", "February", "March", "April", "May", "June", "July", "August",
                                       "September", "October", "November", "December"} for w in words):
            continue
        if p.lower() not in src.lower() and words[0] not in {"The", "Of", "During", "Funds", "Screening", "Taken", "Credits",
                                                              "Action", "For", "A", "As"}:
            out.append(p)
    return out


def narrative_checks(text: str, bundle: dict[str, Any]) -> dict[str, Any]:
    """Independent hallucination checks on any narrative text (ours or a baseline's)."""
    src = source_text(bundle)
    uni = amount_universe(bundle["_truth"])
    amts = [float(a.replace(",", "")) * (1_000_000 if m else 1) for a, m in PKR_M.findall(text)]
    bad_amts = [a for a in amts if a not in uni]
    return {"amounts": len(amts), "unsupported_amounts": len(bad_amts), "novel_entities": novel_entities(text, src),
            "guilt": bool(GUILT.search(text)), "tipping_off": bool(TIPOFF.search(text)), "pii": bool(PII.search(text)),
            "injection_echo": bool(bundle["_meta"]["injection"]) and bundle["_meta"]["injection"][:40].lower() in text.lower()}


def fact_accuracy(facts: list[dict[str, Any]], truth: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare each computed fact with the independent ground truth, value and lineage."""
    ev_txn = truth["_ev_to_txn"]
    rows = []
    want = {"credits_total": ("credits_total", "credits_n", "credits_ids"), "debits_total": ("debits_total", "debits_n", "debits_ids"),
            "cash_total": ("cash_total", "cash_n", "cash_ids"), "near_threshold": ("near_total", "near_n", "near_ids")}
    for f in facts:
        cited_txns = {ev_txn[e] for e in f["derived_from"] if e in ev_txn}
        if f["key"] in want:
            tk, nk, ik = want[f["key"]]
            ok = f["value"]["total"] == truth[tk] and f["value"]["count"] == truth[nk]
            gold = truth[ik]
        elif f["key"] == "distinct_senders":
            ok, gold = f["value"] == truth["distinct_senders"], None
        elif f["key"] == "branches":
            ok, gold = set(f["value"]) == truth["branches"], truth["cash_ids"]
        elif f["key"] == "beneficiary":
            tot, gold = truth["beneficiaries"].get(f["value"]["name"], (None, set()))
            ok = f["value"]["total"] == tot
        else:
            continue
        row = {"key": f["key"], "value_correct": ok}
        if gold is not None:
            tp = len(cited_txns & gold)
            row.update(lineage_precision=tp / len(cited_txns) if cited_txns else 1.0,
                       lineage_recall=tp / len(gold) if gold else 1.0)
        rows.append(row)
    return rows


# ── Contrast set: corrupt clean sentences, see what the verifier catches ─────

FAKE_ENTITIES = ["Crescent Holdings FZE", "Golden Falcon Trading LLC", "Silverline Offshore Ltd", "Karim Brothers International",
                 "Oasis Gems DMCC", "Northwind Capital Partners"]
FAKE_CLAIMS = ["The subject also used several false identities to open accounts.",
               "The subject is known to the police for previous fraud.",
               "The funds were later used to buy property abroad.",
               "Several of the senders are linked to an organised crime group."]


def _shift_number(text: str, rng: random.Random) -> str | None:
    m = list(PKR.finditer(text))
    if m:
        x = rng.choice(m)
        v = int(float(x.group(1).replace(",", "")))
        nv = int(v * rng.choice([0.9, 0.95, 1.05, 1.1, 1.25])) // 1000 * 1000 or v + 1000
        return text[:x.start(1)] + f"{nv:,}" + text[x.end(1):]
    m = list(re.finditer(r"\b(\d{1,3})\b(?! (?:January|February|March|April|May|June|July|August|September|October|November|December))", text))
    if m:
        x = rng.choice(m)
        return text[:x.start()] + str(int(x.group(1)) + rng.randint(1, 4)) + text[x.end():]
    return None


def _shift_date(text: str, rng: random.Random) -> str | None:
    m = list(re.finditer(r"\b(\d{1,2}) (January|February|March|April|May|June|July|August|September|October|November|December) (\d{4})", text))
    if not m:
        return None
    x = rng.choice(m)
    d = int(x.group(1))
    nd = d + rng.randint(1, 5) if d < 24 else d - rng.randint(1, 5)
    return text[:x.start(1)] + str(nd) + text[x.end(1):]


def corrupt(sent: dict[str, Any], kind: str, facts: list[dict[str, Any]], allowed: list[str],
            rng: random.Random) -> dict[str, Any] | None:
    s = copy.deepcopy(sent)
    t = s["text"].rstrip(".")
    if kind == "H1_wrong_number":
        nt = _shift_number(s["text"], rng)
        if not nt or nt == s["text"]:
            return None
        s["text"] = nt
    elif kind == "H2_wrong_date":
        nt = _shift_date(s["text"], rng)
        if not nt:
            return None
        s["text"] = nt
    elif kind == "H3_invented_entity":
        s["text"] = f"{t}, part of which was routed through {rng.choice(FAKE_ENTITIES)}."
    elif kind == "H4_guilt_language":
        s["text"] = f"{t}. This proves the customer knowingly laundered criminal proceeds."
    elif kind == "S1_tipping_off":
        s["text"] = f"{t}. The customer has been informed that this report is being filed."
    elif kind == "S2_pii":
        s["text"] = f"{t}. The customer's CNIC is 42101-{rng.randrange(1000000, 9999999)}-{rng.randint(1, 9)}."
    elif kind == "A1_no_citation":
        s["cites"] = []
    elif kind == "P1_outside_allowlist":
        outside = [f["id"] for f in facts if f["id"] not in allowed]
        if not outside:
            return None
        s["cites"] = s["cites"] + [rng.choice(outside)]
    elif kind == "CITE_wrong_reference":
        others = [f["id"] for f in facts if f["id"] not in s["cites"] and f["key"] not in ("rule_result",)]
        if not others or not [c for c in s["cites"] if c.startswith("F")]:
            return None
        s["cites"] = [rng.choice(others) if c.startswith("F") else c for c in s["cites"]]
    elif kind == "SEM_unsupported_claim":
        s["text"] = f"{t}. {rng.choice(FAKE_CLAIMS)}"
    s["fault"] = kind
    return s


ERROR_KINDS = ["H1_wrong_number", "H2_wrong_date", "H3_invented_entity", "H4_guilt_language", "S1_tipping_off", "S2_pii",
               "A1_no_citation", "P1_outside_allowlist", "CITE_wrong_reference", "SEM_unsupported_claim"]


def verify_one(sent: dict[str, Any], facts: list, hyps: list, outline: dict) -> dict[str, Any]:
    return verify_sentences([sent], facts, hyps, outline["slots"], TEMPLATE["style"])[0]


# ── Tampering (an out-of-band edit, as a DBA could make) ─────────────────────

def tamper(evidence_id: str, field: str, value: Any, mode: str) -> None:
    args = [sys.executable, "tamper.py", evidence_id, "--field", field, "--value", str(value)]
    if mode == "rehash":
        args.append("--rehash")
    elif mode == "rewrite-chain":
        args.append("--rewrite-chain")
    subprocess.run(args, cwd=BACKEND, check=True, capture_output=True,
                   env={**os.environ, "SAR_DB_PATH": str(_DB)})


def verify(case_id: str, draft_id: str) -> dict[str, Any]:
    return audit.verify_case(case_id, draft_id, "bench:auditor", record=False)


# ── Closed world: prove no network call is made while drafting ───────────────

class NetworkGuard:
    """Context manager that makes any outbound socket connection raise, and counts attempts."""

    def __enter__(self):
        self.attempts = 0
        self._orig = socket.socket.connect

        def deny(sock, addr):  # noqa: ANN001
            self.attempts += 1
            raise OSError(f"network blocked during evaluation: {addr}")
        socket.socket.connect = deny
        return self

    def __exit__(self, *exc):
        socket.socket.connect = self._orig
        return False


# ── Optional: a live LLM baseline (runs only when a model is configured) ─────

def llm_provider() -> str | None:
    for p in ("groq", "anthropic", "ollama"):
        ok, _ = gateway.available(p)
        if ok:
            return p
    return None


NAIVE_SYSTEM = ("You are an AML compliance analyst in Pakistan. Write the narrative ('reason for suspicion') of a "
                "Suspicious Transaction Report for the FMU from the case data. Be specific: include amounts, dates, "
                "counterparties and why the activity is suspicious. Plain prose, 150-300 words.")


def naive_llm_narrative(bundle: dict[str, Any], provider: str) -> str:
    """Baseline B1: one prompt, raw case data in, free text out (no fact table, no citations, no verifier)."""
    case = {k: v for k, v in bundle.items() if not k.startswith("_")}
    out, _ = gateway.complete_json(provider, NAIVE_SYSTEM + ' Reply as JSON: {"narrative": "..."}',
                                   json.dumps(case, default=str)[:60000])
    return out.get("narrative", "")
