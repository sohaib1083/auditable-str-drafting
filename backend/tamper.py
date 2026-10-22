"""Simulate an out-of-band attacker editing the evidence ledger directly in SQLite.

    python tamper.py EV-1001-0010 --field amount --value 1500000
    python tamper.py EV-1001-0010 --field amount --value 1500000 --rehash
    python tamper.py EV-1001-0010 --field amount --value 1500000 --rewrite-chain

The application can never do this: the evidence table's triggers reject
UPDATE. So, like a DBA with direct file access, this script drops the trigger,
edits the row and puts the trigger back.

  (default)        edit content only            → caught: content no longer matches content_hash
  --rehash         also recompute content_hash  → caught: entry_hash no longer matches
  --rewrite-chain  recompute every later hash   → chain walks clean, but caught by the evidence
                                                  head anchored in the audit trail at draft time
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from app import chain
from app.config import settings

TRIGGER = ("CREATE TRIGGER IF NOT EXISTS evidence_no_update BEFORE UPDATE ON evidence "
           "BEGIN SELECT RAISE(ABORT, 'evidence ledger is append-only'); END;")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("evidence_id")
    ap.add_argument("--field", required=True)
    ap.add_argument("--value", required=True)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--rehash", action="store_true")
    g.add_argument("--rewrite-chain", action="store_true")
    a = ap.parse_args()

    conn = sqlite3.connect(settings.db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM evidence WHERE id=?", (a.evidence_id,)).fetchone()
    if not r:
        sys.exit(f"No evidence item {a.evidence_id}")
    content = json.loads(r["content_json"])
    if a.field not in content:
        sys.exit(f"{a.evidence_id} has no field '{a.field}'. Fields: {', '.join(content)}")
    old = content[a.field]
    try:
        new: object = type(old)(a.value) if not isinstance(old, str) else a.value
    except ValueError:
        new = a.value
    content[a.field] = new

    conn.execute("BEGIN")
    conn.execute("DROP TRIGGER IF EXISTS evidence_no_update")
    if a.rewrite_chain:
        rows = conn.execute("SELECT * FROM evidence WHERE case_id=? AND seq>=? ORDER BY seq", (r["case_id"], r["seq"])).fetchall()
        prev = r["prev_hash"]
        for row in rows:
            c = content if row["id"] == a.evidence_id else json.loads(row["content_json"])
            c_hash = chain.content_hash(c)
            e_hash = chain.entry_hash(c_hash, prev, row["captured_at"])
            conn.execute("UPDATE evidence SET content_json=?, content_hash=?, prev_hash=?, entry_hash=? WHERE id=?",
                         (json.dumps(c, ensure_ascii=False), c_hash, prev, e_hash, row["id"]))
            prev = e_hash
    elif a.rehash:
        conn.execute("UPDATE evidence SET content_json=?, content_hash=? WHERE id=?",
                     (json.dumps(content, ensure_ascii=False), chain.content_hash(content), a.evidence_id))
    else:
        conn.execute("UPDATE evidence SET content_json=? WHERE id=?", (json.dumps(content, ensure_ascii=False), a.evidence_id))
    conn.execute(TRIGGER)
    conn.execute("COMMIT")
    mode = "rewrite-chain" if a.rewrite_chain else "rehash" if a.rehash else "content-only"
    print(f"Tampered {a.evidence_id} ({mode}): {a.field} {old!r} → {new!r}. Now press Verify in the UI.")


if __name__ == "__main__":
    main()
