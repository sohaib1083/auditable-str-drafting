"""SQLite storage.

SQLite (not Postgres) is deliberate for the MVP: a single-file, zero-admin
store is what an on-prem pilot at a PSP or mid-size bank can actually install,
and it makes the tamper demo concrete ("open the .db file and edit a row").

The evidence and trail tables are append-only at the storage layer: triggers
reject UPDATE and DELETE. That is the second barrier from
03-technical-breakdown.md §2. It stops the application from ever silently
rewriting its own evidence; tampering has to happen out-of-band (dropping the
trigger), and the hash chain then catches it.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('analyst','mlro','auditor')),
  pw_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
  id TEXT PRIMARY KEY, ref TEXT NOT NULL, tenant_id TEXT NOT NULL, title TEXT NOT NULL,
  summary TEXT NOT NULL, jurisdiction TEXT NOT NULL, subject_json TEXT NOT NULL,
  alert_json TEXT NOT NULL, status TEXT NOT NULL, opened_at TEXT NOT NULL, detected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), seq INTEGER NOT NULL,
  type TEXT NOT NULL, label TEXT NOT NULL, source TEXT NOT NULL, source_ref TEXT NOT NULL,
  content_json TEXT NOT NULL, content_hash TEXT NOT NULL, prev_hash TEXT NOT NULL,
  entry_hash TEXT NOT NULL, captured_at TEXT NOT NULL, captured_by TEXT NOT NULL,
  UNIQUE (case_id, seq)
);
CREATE TRIGGER IF NOT EXISTS evidence_no_update BEFORE UPDATE ON evidence
  BEGIN SELECT RAISE(ABORT, 'evidence ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_no_delete BEFORE DELETE ON evidence
  BEGIN SELECT RAISE(ABORT, 'evidence ledger is append-only'); END;

CREATE TABLE IF NOT EXISTS trail (
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, seq INTEGER NOT NULL,
  kind TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, draft_id TEXT,
  detail_json TEXT NOT NULL, content_hash TEXT NOT NULL, prev_hash TEXT NOT NULL,
  entry_hash TEXT NOT NULL, at TEXT NOT NULL, UNIQUE (case_id, seq)
);
CREATE TRIGGER IF NOT EXISTS trail_no_update BEFORE UPDATE ON trail
  BEGIN SELECT RAISE(ABORT, 'audit trail is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trail_no_delete BEFORE DELETE ON trail
  BEGIN SELECT RAISE(ABORT, 'audit trail is append-only'); END;

CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), sar_required INTEGER NOT NULL,
  reason TEXT NOT NULL, suspicion_formed_at TEXT NOT NULL, deadline_at TEXT NOT NULL,
  decided_by TEXT NOT NULL, decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), version INTEGER NOT NULL,
  status TEXT NOT NULL, jurisdiction TEXT NOT NULL, template_version TEXT NOT NULL,
  provider TEXT NOT NULL, endpoint_json TEXT NOT NULL, inject_faults INTEGER NOT NULL DEFAULT 0,
  stage TEXT NOT NULL DEFAULT 'QUEUED', pct INTEGER NOT NULL DEFAULT 0,
  facts_json TEXT, hypotheses_json TEXT, outline_json TEXT, abstain_json TEXT,
  removed_json TEXT, checklist_json TEXT, stats_json TEXT, structured_json TEXT,
  evidence_head TEXT, content_hash TEXT, error TEXT,
  requested_by TEXT NOT NULL, maker_id TEXT, checker_id TEXT, attestation TEXT,
  submitted_at TEXT, approved_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sentences (
  id TEXT PRIMARY KEY, draft_id TEXT NOT NULL REFERENCES drafts(id), idx INTEGER NOT NULL,
  section TEXT NOT NULL, text TEXT NOT NULL, cites_json TEXT NOT NULL,
  verdict_json TEXT NOT NULL, status TEXT NOT NULL, origin TEXT NOT NULL,
  review_status TEXT NOT NULL DEFAULT 'pending', review_reason TEXT, history_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS filings (
  id TEXT PRIMARY KEY, draft_id TEXT NOT NULL REFERENCES drafts(id), channel TEXT NOT NULL,
  payload TEXT NOT NULL, payload_hash TEXT NOT NULL, validation_json TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at TEXT NOT NULL, fiu_reference TEXT, submitted_at TEXT
);
CREATE TABLE IF NOT EXISTS external_links (
  case_id TEXT NOT NULL REFERENCES cases(id), system TEXT NOT NULL, ext_case_id TEXT NOT NULL,
  ext_task_id TEXT, url TEXT NOT NULL, linked_by TEXT NOT NULL, linked_at TEXT NOT NULL,
  PRIMARY KEY (case_id, system)
);
CREATE INDEX IF NOT EXISTS ix_evidence_case ON evidence(case_id, seq);
CREATE INDEX IF NOT EXISTS ix_trail_case ON trail(case_id, seq);
CREATE INDEX IF NOT EXISTS ix_sentences_draft ON sentences(draft_id, idx);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    """One connection per unit of work. Writes inside run in one transaction."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    out = dict(r)
    for k in list(out):
        if k.endswith("_json") and out[k] is not None:
            out[k[:-5]] = json.loads(out.pop(k))
    return out


def rows(rs: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [row(r) for r in rs]  # type: ignore[misc]


def dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)
