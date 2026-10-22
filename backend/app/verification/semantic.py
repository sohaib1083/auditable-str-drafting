"""Semantic support check (layer 3 of the verifier).

The deterministic checks (numbers, dates, entities, intent language, citations) cannot
see a claim that contains no number or name, such as "the subject is known to the
police". This layer asks an independent model, at temperature 0, whether the cited
evidence entails every factual statement in the sentence. It runs only when
SAR_SEMANTIC_MODEL is set, and only on sentences the deterministic layers passed.

The judge sees only the sentence and the display text of the facts and hypotheses
the sentence cites, never the draft as a whole, so it cannot be anchored by
surrounding text. This module sends the text as is: in a deployment with a hosted
judge, route it through the gateway's pseudonymiser first (the benchmark data is synthetic).
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

SEMANTIC_VERSION = "2"

SYSTEM = (
    "You check one sentence of a suspicious transaction report against the evidence it cites.\n"
    "Each evidence line is '<fact label>: <fact text>'. Decide whether the sentence adds any FACTUAL CLAIM that the "
    "cited evidence does not support.\n"
    "These are acceptable and must NOT be treated as unsupported:\n"
    "- framing or connective words that only restate what a cited fact describes (e.g. 'Of the outgoing funds' for a "
    "fact about debits or beneficiaries; 'Funds did not remain in the account' for a fact showing most credits were "
    "debited soon after);\n"
    "- hedged characterisations of suspicion ('consistent with', 'indicative of', 'suspected') when a cited typology "
    "hypothesis supports them;\n"
    "- the reporting entity's statements about its own position or process (e.g. 'the reporting entity has not "
    "established the underlying offence', 'this report is based on the information available');\n"
    "- wording that names what a fact is (e.g. 'opened on' for an account-opening date).\n"
    "A claim IS unsupported if it states something about the subject, the funds, other parties, past conduct or "
    "events that the cited evidence does not state (e.g. criminal history, false identities, property purchases, "
    "links to organised crime, travel, reasons or sources of funds not in the evidence).\n"
    'Reply as JSON: {"supported": true or false, "unsupported_part": "<the unsupported claim, or empty>"}')


def enabled() -> bool:
    return bool(os.environ.get("SAR_SEMANTIC_MODEL"))


def check(text: str, evidence: list[str]) -> dict[str, Any]:
    """Return {"supported": bool, "unsupported_part": str, "model": str}; raises RuntimeError on API failure."""
    model = os.environ["SAR_SEMANTIC_MODEL"]
    user = "CITED EVIDENCE:\n" + "\n".join(f"- {e}" for e in evidence) + f"\n\nSENTENCE:\n{text}"
    body: dict[str, Any] = {"model": model, "temperature": 0, "max_completion_tokens": 600,
                            "response_format": {"type": "json_object"},
                            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]}
    if "gpt-oss" in model:
        body["reasoning_effort"] = "low"
    if "qwen" in model:
        body["reasoning_format"] = "hidden"
        body["reasoning_effort"] = "none"
    headers = {"Authorization": "Bearer " + os.environ["GROQ_API_KEY"]}
    for _ in range(12):
        try:
            r = httpx.post("https://api.groq.com/openai/v1/chat/completions", json=body, headers=headers, timeout=120)
        except httpx.HTTPError:
            time.sleep(10)
            continue
        if r.status_code == 429:
            if "per day" in r.text or "TPD" in r.text:
                raise RuntimeError("GROQ_DAILY_LIMIT: " + r.text[:200])
            time.sleep(float(r.headers.get("retry-after") or 15) + 1)
            continue
        if r.status_code >= 500:
            time.sleep(10)
            continue
        r.raise_for_status()
        out = json.loads(r.json()["choices"][0]["message"]["content"])
        sup = out.get("supported")
        sup = sup if isinstance(sup, bool) else str(sup).lower() == "true"
        return {"supported": sup, "unsupported_part": str(out.get("unsupported_part") or ""), "model": model, "version": SEMANTIC_VERSION}
    raise RuntimeError("semantic check: API unavailable after retries")
