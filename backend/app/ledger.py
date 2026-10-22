"""Evidence ledger and audit trail writers.

Only the orchestrator and the API layer call these. Agents never get a
handle to them (least agency: agents have no write tools).
"""
from __future__ import annotations

import sqlite3
from typing import Any

from app import chain
from app.db import dumps, row, rows


# ── Evidence ledger ──────────────────────────────────────────────────────────

def append_evidence(conn: sqlite3.Connection, case_id: str, etype: str, label: str,
                    source: str, source_ref: str, content: dict[str, Any], captured_by: str) -> dict[str, Any]:
    tail = conn.execute("SELECT seq, entry_hash FROM evidence WHERE case_id=? ORDER BY seq DESC LIMIT 1",
                        (case_id,)).fetchone()
    seq = (tail["seq"] + 1) if tail else 1
    prev = tail["entry_hash"] if tail else chain.genesis(case_id)
    h = chain.link(content, prev)
    ev_id = f"EV-{case_id.split('-')[-1]}-{seq:04d}"
    conn.execute(
        "INSERT INTO evidence (id, case_id, seq, type, label, source, source_ref, content_json, content_hash,"
        " prev_hash, entry_hash, captured_at, captured_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ev_id, case_id, seq, etype, label, source, source_ref, dumps(content), h["content_hash"],
         h["prev_hash"], h["entry_hash"], h["timestamp"], captured_by))
    return {"id": ev_id, "seq": seq, **h}


def list_evidence(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    return rows(conn.execute("SELECT * FROM evidence WHERE case_id=? ORDER BY seq", (case_id,)).fetchall())


def evidence_head(conn: sqlite3.Connection, case_id: str) -> str | None:
    r = conn.execute("SELECT entry_hash FROM evidence WHERE case_id=? ORDER BY seq DESC LIMIT 1", (case_id,)).fetchone()
    return r["entry_hash"] if r else None


# ── Audit trail (agent runs, human review actions, decisions) ────────────────

def append_trail(conn: sqlite3.Connection, case_id: str, kind: str, actor: str, action: str,
                 detail: dict[str, Any], draft_id: str | None = None) -> dict[str, Any]:
    tail = conn.execute("SELECT seq, entry_hash FROM trail WHERE case_id=? ORDER BY seq DESC LIMIT 1",
                        (case_id,)).fetchone()
    seq = (tail["seq"] + 1) if tail else 1
    prev = tail["entry_hash"] if tail else chain.genesis(f"trail:{case_id}")
    signed = {"kind": kind, "actor": actor, "action": action, "draft_id": draft_id, "detail": detail}
    h = chain.link(signed, prev)
    conn.execute(
        "INSERT INTO trail (case_id, seq, kind, actor, action, draft_id, detail_json, content_hash, prev_hash,"
        " entry_hash, at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, seq, kind, actor, action, draft_id, dumps(detail), h["content_hash"], h["prev_hash"],
         h["entry_hash"], h["timestamp"]))
    return {"seq": seq, **h}


def list_trail(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    return rows(conn.execute("SELECT * FROM trail WHERE case_id=? ORDER BY seq", (case_id,)).fetchall())


def get_case(conn: sqlite3.Connection, case_id: str) -> dict[str, Any] | None:
    return row(conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone())
