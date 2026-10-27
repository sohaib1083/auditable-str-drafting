"""Typology Analyst (agent 3): Facts + Tazama result + catalogue T → hypotheses H.

In the MVP this agent is deterministic: indicator rules from the catalogue are
evaluated over the fact metrics. That makes hypothesis confidence reproducible
and auditable (A8). Each hypothesis records its supporting AND counter
evidence; counter-evidence lowers confidence rather than being ignored, which
is the defence against suspicion inflation (S3).
"""
from __future__ import annotations

import ast
import operator
from typing import Any

from app import chain
from app.pipeline.facts import FactTable
from app.templates import load_catalogue

_OPS = {ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Lt: operator.lt, ast.LtE: operator.le,
        ast.Eq: operator.eq, ast.NotEq: operator.ne}


def eval_rule(expr: str, metrics: dict[str, Any]) -> bool:
    """Evaluate a catalogue rule. Only comparisons, and/or, names and numbers
    are allowed, so a catalogue file can never execute code."""
    def ev(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.BoolOp):
            vals = [ev(v) for v in node.values]
            return all(vals) if isinstance(node.op, ast.And) else any(vals)
        if isinstance(node, ast.Compare):
            left = ev(node.left)
            for op, comp in zip(node.ops, node.comparators):
                right = ev(comp)
                if not _OPS[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.Name):
            if node.id in ("true", "false"):
                return node.id == "true"
            return metrics.get(node.id, 0)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"Disallowed expression in rule: {expr}")
    return bool(ev(ast.parse(expr, mode="eval")))


def _metrics(ft: FactTable, typ: dict[str, Any]) -> dict[str, Any]:
    return {**ft.metrics, "tazama_match": ft.metrics.get("tazama_typology") in typ["tazama_typologies"]}


def evaluation_record(ft: FactTable) -> list[dict[str, Any]]:
    """Every indicator and counter-indicator checked for every typology, fired or not (A8).

    A hypothesis lists only the indicators that fired, so on its own it cannot show that
    a counter-indicator was checked and found absent. This record, written to the trail, can."""
    cat = load_catalogue()
    return [{"typology": typ["id"], "catalogue_version": cat["version"],
             "support": [{"id": i["id"], "fired": eval_rule(i["rule"], _metrics(ft, typ))} for i in typ["indicators"]],
             "counter": [{"id": i["id"], "fired": eval_rule(i["rule"], _metrics(ft, typ))} for i in typ["counter_indicators"]]}
            for typ in cat["typologies"]]


def analyse(ft: FactTable, abstain_confidence: float) -> list[dict[str, Any]]:
    cat = load_catalogue()
    out: list[dict[str, Any]] = []
    for n, typ in enumerate(cat["typologies"], 1):
        metrics = _metrics(ft, typ)

        def resolve(keys: list[str]) -> list[str]:
            return [fid for k in keys for fid in ft.by_key.get(k, [])]

        support, counter = [], []
        for ind in typ["indicators"]:
            if eval_rule(ind["rule"], metrics):
                support.append({"id": ind["id"], "text": ind["text"], "weight": ind["weight"], "fact_ids": resolve(ind["facts"])})
        for ind in typ["counter_indicators"]:
            if eval_rule(ind["rule"], metrics):
                counter.append({"id": ind["id"], "text": ind["text"], "weight": ind["weight"], "fact_ids": resolve(ind["facts"])})
        s = sum(i["weight"] for i in support)
        if metrics["tazama_match"]:
            s += 0.05  # corroboration from the detection engine, deliberately small
        c = sum(i["weight"] for i in counter)
        conf = round(max(0.0, min(1.0, s) - c), 2)
        hyp = {
            "id": f"H{n}", "typology_id": typ["id"], "name": typ["name"], "short": typ["short"],
            "confidence": conf, "supported": conf >= abstain_confidence,
            "support": support, "counter": counter,
            "support_fact_ids": sorted({f for i in support for f in i["fact_ids"]}),
            "counter_fact_ids": sorted({f for i in counter for f in i["fact_ids"]}),
            "tazama_match": metrics["tazama_match"], "catalogue_version": cat["version"],
        }
        hyp["display"] = (f"{typ['name']} — confidence {conf:.2f}; indicators: "
                          + (", ".join(i["id"] for i in support) or "none")
                          + ("; counter-indicators: " + ", ".join(i["id"] for i in counter) if counter else ""))
        hyp["hash"] = chain.content_hash({k: v for k, v in hyp.items() if k != "id"})
        out.append(hyp)
    return out
