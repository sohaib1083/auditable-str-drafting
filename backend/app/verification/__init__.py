"""Independent Verification Layer.

Imports nothing from app.pipeline (the generator). It reads the stored draft,
facts, hypotheses and evidence, and judges them. The orchestrator calls
`verify_sentences` during drafting; the audit endpoint calls the same
functions later on stored data, which is what makes verdicts replayable (A7).
"""
from __future__ import annotations

from typing import Any

from app.verification.claims import VERIFIER_VERSION, build_lexicon, verify_sentence


def verify_sentences(sentences: list[dict[str, Any]], facts: list[dict[str, Any]], hyps: list[dict[str, Any]],
                     slots: list[dict[str, Any]], style: dict[str, Any]) -> list[dict[str, Any]]:
    fx = {f["id"]: f for f in facts}
    hx = {h["id"]: h for h in hyps}
    allowed = {s["slot"]: s["allowed"] for s in slots}
    lex = build_lexicon(facts)
    return [verify_sentence(s, fx, hx, allowed.get(s.get("slot") or ""), lex, style) for s in sentences]


def completeness(sentences: list[dict[str, Any]], facts: list[dict[str, Any]], hyps: list[dict[str, Any]],
                 template: dict[str, Any]) -> list[dict[str, Any]]:
    """Required-element checklist for the jurisdiction (6e, rule-based in the MVP)."""
    fkey = {f["id"]: f["key"] for f in facts}
    supported = {h["id"] for h in hyps if h["supported"]}
    active = [s for s in sentences if s["status"] == "active"]
    out = []
    for el in template["required_elements"]:
        hits = []
        for key in el["fact_keys"]:
            ids = [s["id"] for s in active if any((c in supported) if key == "@hypothesis" else fkey.get(c) == key for c in s["cites"])]
            hits.append(ids)
        met = all(hits) if el["mode"] == "all" else any(hits)
        out.append({"id": el["id"], "label": el["label"], "status": "met" if met else "missing",
                    "sentence_ids": sorted({i for h in hits for i in h})})
    text = " ".join(s["text"] for s in active).lower()
    tip = [p for p in template["style"]["tipping_off"] if p in text]
    out.append({"id": "no_tipping_off", "label": "No tipping-off language", "status": "missing" if tip else "met", "sentence_ids": []})
    length = sum(len(s["text"]) + 1 for s in active)
    out.append({"id": "length", "label": f"Narrative within {template['narrative']['max_chars']} characters ({length})",
                "status": "met" if length <= template["narrative"]["max_chars"] else "missing", "sentence_ids": []})
    return out


__all__ = ["VERIFIER_VERSION", "verify_sentences", "completeness"]
