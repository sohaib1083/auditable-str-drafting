"""Hash-chain primitive used by the evidence ledger and the audit trail.

Ported from the Sanad MVP (third application of the same construction):

    content_hash(n) = SHA256(canonical(content(n)))
    entry_hash(n)   = SHA256(content_hash(n) || prev_hash(n) || timestamp(n))
    prev_hash(n)    = entry_hash(n-1);  prev_hash(0) = genesis(chain_id)

Altering any past entry changes its content_hash, which no longer matches the
stored entry_hash, and every later link depends on it. The verification
package re-implements the walk independently (see verification/integrity.py)
so a bug here cannot silently pass both.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


def canonical(payload: Any) -> str:
    """Deterministic JSON: identical content always serialises identically."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(content: Any) -> str:
    return sha256(canonical(content))


def genesis(chain_id: str) -> str:
    """Per-chain genesis so two cases' chains can never be spliced together."""
    return sha256(f"genesis:{chain_id}")


def entry_hash(c_hash: str, prev_hash: str, timestamp: str) -> str:
    return sha256(f"{c_hash}|{prev_hash}|{timestamp}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def link(content: Any, prev_hash: str, timestamp: str | None = None) -> dict[str, str]:
    """Compute the three hash fields for a new chain entry."""
    ts = timestamp or now_iso()
    c_hash = content_hash(content)
    return {"content_hash": c_hash, "prev_hash": prev_hash, "entry_hash": entry_hash(c_hash, prev_hash, ts), "timestamp": ts}
