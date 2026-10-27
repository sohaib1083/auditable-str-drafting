"""Audit pack: everything an examiner needs to test one SAR, in one export.

JSON for machines (diffable, re-verifiable) and a self-contained HTML page
for people (prints to PDF from any browser). Contents map to the audit
questions in 02-problem-definition.md §3.

Version 1.1 carries the whole evidence chain and fact table (not only the cited
parts), the planner's allow-lists, the reasons for a decline, an actor directory
and the release manifest. Without them an independent auditor cannot walk the
evidence chain, confirm the drafter's input, re-run the verifier or find the
benchmark results for the release (experiment E7).
"""
from __future__ import annotations

import html
import json
from typing import Any

from app import chain
from app.config import BASE_DIR

PACK_VERSION = "1.1"


def release_manifest() -> dict[str, Any] | None:
    """The release this deployment runs (component versions and its published benchmark results), if one is installed."""
    p = BASE_DIR / "release.json"
    return json.loads(p.read_text()) if p.exists() else None


def build_json(case: dict[str, Any], draft: dict[str, Any], sentences: list[dict[str, Any]], evidence: list[dict[str, Any]],
               trail: list[dict[str, Any]], decision: dict[str, Any] | None, filings: list[dict[str, Any]],
               integrity: dict[str, Any], users: dict[str, str],
               actors: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    active = [s for s in sentences if s["status"] == "active"]
    cited = {c for s in active for c in s["cites"]}
    facts = draft.get("facts") or []
    pack = {
        "pack_version": PACK_VERSION,
        "generated_at": chain.now_iso(),
        "case": {k: case[k] for k in ("id", "ref", "title", "jurisdiction", "status", "detected_at", "opened_at")},
        "A5_decision": decision and {**decision, "decided_by": users.get(decision["decided_by"], decision["decided_by"])},
        "draft": {"id": draft["id"], "version": draft["version"], "status": draft["status"],
                  "template": draft["template_version"], "provider": draft["provider"], "endpoint": draft["endpoint"],
                  "content_hash": draft["content_hash"], "evidence_head_at_generation": draft["evidence_head"],
                  "maker": users.get(draft["maker_id"] or "", None), "checker": users.get(draft["checker_id"] or "", None),
                  "attestation": draft["attestation"], "submitted_at": draft["submitted_at"], "approved_at": draft["approved_at"],
                  "stats": draft["stats"]},
        "A1_claims": [{"id": s["id"], "section": s["section"], "text": s["text"], "cites": s["cites"],
                       "status": s["status"], "review": s["review_status"], "reason": s["review_reason"],
                       "A2_verdict": s["verdict"], "history": s["history"]} for s in sentences],
        "removed_by_verifier": draft.get("removed") or [],
        "outline": draft.get("outline"),
        "abstain": draft.get("abstain"),
        "facts_scope": "full",
        "cited_fact_ids": sorted(c for c in cited if c.startswith("F")),
        "facts": facts,
        "A8_hypotheses": draft.get("hypotheses") or [],
        "evidence_scope": "full",
        "evidence": [{k: e[k] for k in ("id", "seq", "type", "label", "source", "source_ref", "content", "content_hash",
                                        "prev_hash", "entry_hash", "captured_at")} for e in evidence],
        "A3_A4_trail": [{k: t[k] for k in ("seq", "kind", "actor", "action", "draft_id", "detail", "content_hash",
                                           "prev_hash", "entry_hash", "at")} for t in trail],
        "filings": [{k: f[k] for k in ("id", "channel", "payload_hash", "validation", "created_at", "fiu_reference", "submitted_at")}
                    for f in filings],
        "A6_A7_integrity": integrity,
        "actors": actors,
        "release": release_manifest(),
    }
    pack["pack_hash"] = chain.content_hash(pack)
    return pack


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def build_html(pack: dict[str, Any]) -> str:
    d, c, integ = pack["draft"], pack["case"], pack["A6_A7_integrity"]
    facts = {f["id"]: f for f in pack["facts"]}
    hyps = {h["id"]: h for h in pack["A8_hypotheses"]}
    ev = {e["id"]: e for e in pack["evidence"]}

    def claim_rows() -> str:
        out = []
        for s in pack["A1_claims"]:
            if s["status"] != "active":
                continue
            cites = []
            for cid in s["cites"]:
                if cid in facts:
                    f = facts[cid]
                    cites.append(f"<b>{cid}</b> {_e(f['display'])} <span class=m>[{f['method']} · {f['tool']}@{f['tool_version']}] ← "
                                 + ", ".join(_e(x) for x in f["derived_from"][:6]) + (" …" if len(f["derived_from"]) > 6 else "") + "</span>")
                elif cid in hyps:
                    cites.append(f"<b>{cid}</b> {_e(hyps[cid]['display'])}")
            checks = ", ".join(f"{x['code']}:{x['status']}" for x in s["A2_verdict"]["checks"])
            out.append(f"<tr><td>{_e(s['id'].split('-')[-1])}</td><td>{_e(s['text'])}<div class=m>{checks}</div></td>"
                       f"<td>{'<br>'.join(cites)}</td><td>{_e(s['review'])}<br><span class=m>{_e(s['reason'])}</span></td></tr>")
        return "".join(out)

    trail = "".join(f"<tr><td>{t['seq']}</td><td>{_e(t['at'][:19])}</td><td>{_e(t['actor'])}</td><td>{_e(t['action'])}</td>"
                    f"<td class=h>{_e(t['entry_hash'][:16])}…</td></tr>" for t in pack["A3_A4_trail"])
    evid = "".join(f"<tr><td>{_e(e['id'])}</td><td>{_e(e['label'])}</td><td>{_e(e['source'])}</td>"
                   f"<td class=h>{_e(e['content_hash'][:16])}…</td><td class=h>{_e(e['entry_hash'][:16])}…</td></tr>" for e in ev.values())
    removed = "".join(f"<li>{_e(r['text'])} <span class=m>— " + "; ".join(_e(p['message']) for p in r["problems"]) + "</span></li>"
                      for r in pack["removed_by_verifier"]) or "<li>None</li>"
    dec = pack["A5_decision"] or {}
    dr = integ.get("draft") or {}
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Audit pack {_e(c['ref'])}</title><style>
body{{font:13px/1.5 system-ui,sans-serif;color:#15202b;margin:24px auto;max-width:1100px;padding:0 16px}}
h1{{font-size:20px;margin:0}}h2{{font-size:15px;margin:26px 0 8px;border-bottom:1px solid #ccd;padding-bottom:4px}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #dde;padding:5px 7px;vertical-align:top;text-align:left}}
th{{background:#f2f4f8}}.m{{color:#667;font-size:11.5px}}.h{{font-family:ui-monospace,monospace;font-size:11px}}
.ok{{color:#1d6b3f;font-weight:600}}.bad{{color:#b3261e;font-weight:600}}.kv td:first-child{{width:220px;color:#556}}
</style></head><body>
<h1>SAR/STR audit pack — {_e(c['ref'])} ({_e(c['id'])})</h1>
<div class=m>Generated {_e(pack['generated_at'])} · pack hash <span class=h>{_e(pack['pack_hash'])}</span> · synthetic demonstration data</div>
<h2>Summary</h2><table class=kv>
<tr><td>Case</td><td>{_e(c['title'])} · {_e(c['jurisdiction'])}</td></tr>
<tr><td>Draft</td><td>{_e(d['id'])} v{d['version']} · {_e(d['status'])} · template {_e(d['template'])} · model endpoint {_e(d['endpoint'].get('id'))} ({_e(d['endpoint'].get('location'))})</td></tr>
<tr><td>A5 SAR decision</td><td>{'SAR required' if dec.get('sar_required') else '—'} · {_e(dec.get('reason'))}<br>
suspicion formed {_e(dec.get('suspicion_formed_at'))} · deadline {_e(dec.get('deadline_at'))} · by {_e(dec.get('decided_by'))}</td></tr>
<tr><td>A4 Maker / checker</td><td>{_e(d['maker'])} → {_e(d['checker'])} · approved {_e(d['approved_at'])}</td></tr>
<tr><td>Attestation</td><td>{_e(d['attestation'])}</td></tr>
<tr><td>Content hash</td><td class=h>{_e(d['content_hash'])}</td></tr>
<tr><td>A6 Integrity</td><td class="{'ok' if integ['verdict']=='PASS' else 'bad'}">{_e(integ['verdict'])} — evidence chain {_e(integ['chain_integrity'])}, trail {_e(integ['trail_integrity'])}
{'<br>' + '<br>'.join(_e(p) for p in integ['problems']) if integ['problems'] else ''}</td></tr>
<tr><td>Auditability score</td><td>{_e(dr.get('auditability_score'))} ({_e(dr.get('sentences_fully_cited'))}/{_e(dr.get('sentences_total'))} sentences cited, {_e(dr.get('citations_unresolved'))} unresolved citations)</td></tr>
</table>
<h2>A1/A2 Narrative claims, citations and verifier verdicts</h2>
<table><tr><th>#</th><th>Sentence</th><th>Cited facts → evidence</th><th>Review</th></tr>{claim_rows()}</table>
<h2>Removed by the verification layer before human review</h2><ul>{removed}</ul>
<h2>A8 Typology hypotheses (support and counter-evidence)</h2><table><tr><th>ID</th><th>Hypothesis</th><th>Supported</th></tr>
{''.join(f"<tr><td>{h['id']}</td><td>{_e(h['display'])}</td><td>{'yes' if h['supported'] else 'no'}</td></tr>" for h in pack['A8_hypotheses'])}</table>
<h2>Evidence ledger (full chain)</h2><table><tr><th>ID</th><th>Item</th><th>Source</th><th>Content hash</th><th>Entry hash</th></tr>{evid}</table>
<h2>A3/A4 Hash-chained trail</h2><table><tr><th>#</th><th>At (UTC)</th><th>Actor</th><th>Action</th><th>Entry hash</th></tr>{trail}</table>
</body></html>"""
