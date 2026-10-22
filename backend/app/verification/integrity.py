"""Independent integrity checks (audit questions A1, A6, A7).

This module deliberately re-implements the hash-chain walk instead of
importing app.chain: if the writer and the checker shared code, one bug could
pass both. Only hashlib and json are shared, and the construction is
re-stated here from the spec in 03-technical-breakdown.md §2.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _canon(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def walk_chain(entries: list[dict[str, Any]], genesis_label: str, content_of: Any, ts_field: str) -> dict[str, Any]:
    """Recompute every link. Report each broken entry and why, not just a boolean."""
    prev = _h(f"genesis:{genesis_label}")
    problems: list[dict[str, Any]] = []
    first_break: str | None = None
    expected_seq = 1
    for e in entries:
        eid = str(e.get("id"))
        if e["seq"] != expected_seq:
            problems.append({"id": eid, "seq": e["seq"], "kind": "gap",
                             "message": f"Sequence jumps from {expected_seq - 1} to {e['seq']}: entries were deleted."})
        expected_seq = e["seq"] + 1
        recomputed_content = _h(_canon(content_of(e)))
        kinds = []
        if recomputed_content != e["content_hash"]:
            kinds.append(("content_modified", "Content no longer matches its recorded hash: the record was edited after it was written."))
        if e["prev_hash"] != prev:
            kinds.append(("link_broken", "Does not link to the previous entry's hash."))
        if _h(f"{e['content_hash']}|{e['prev_hash']}|{e[ts_field]}") != e["entry_hash"]:
            kinds.append(("hash_rewritten", "Entry hash does not match its own fields: hashes were rewritten."))
        for kind, msg in kinds:
            problems.append({"id": eid, "seq": e["seq"], "kind": kind, "message": msg})
        if kinds and first_break is None:
            first_break = eid
        prev = e["entry_hash"]
    return {"intact": not problems, "entries": len(entries), "first_break": first_break,
            "problems": problems, "head": entries[-1]["entry_hash"] if entries else None}


def verify_evidence_chain(case_id: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    return walk_chain(evidence, case_id, lambda e: e["content"], "captured_at")


def verify_trail_chain(case_id: str, trail: list[dict[str, Any]]) -> dict[str, Any]:
    def content(e: dict[str, Any]) -> dict[str, Any]:
        return {"kind": e["kind"], "actor": e["actor"], "action": e["action"], "draft_id": e["draft_id"], "detail": e["detail"]}
    for n, e in enumerate(trail):
        e.setdefault("id", f"T{e['seq']}")
    return walk_chain(trail, f"trail:{case_id}", content, "at")


def lineage_impact(broken_ids: set[str], facts: list[dict[str, Any]], hyps: list[dict[str, Any]],
                   sentences: list[dict[str, Any]]) -> dict[str, Any]:
    """Which facts, hypotheses and sentences rest on evidence that failed its integrity check."""
    tainted_facts = {f["id"] for f in facts if broken_ids & set(f["derived_from"])}
    tainted_hyps = {h["id"] for h in hyps if tainted_facts & set(h["support_fact_ids"] + h["counter_fact_ids"])}
    affected = [s["id"] for s in sentences if s["status"] == "active" and set(s["cites"]) & (tainted_facts | tainted_hyps)]
    return {"facts": sorted(tainted_facts), "hypotheses": sorted(tainted_hyps), "sentences": affected}


def verify_fact_hashes(facts: list[dict[str, Any]]) -> list[str]:
    """Facts whose stored hash no longer matches their own content."""
    bad = []
    for f in facts:
        body = {k: v for k, v in f.items() if k not in ("id", "hash")}
        if _h(_canon(body)) != f["hash"]:
            bad.append(f["id"])
    return bad
