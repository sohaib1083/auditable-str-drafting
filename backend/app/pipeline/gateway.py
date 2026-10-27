"""Model gateway and endpoint registry (audit question A9).

Every model call goes through here and is attributed to a registered endpoint
recording where inference runs and whether data leaves the institution's
boundary. Endpoints outside the boundary only ever receive pseudonymised
text (see pii.py).
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from app import chain
from app.config import settings


@dataclass(frozen=True)
class Endpoint:
    id: str
    provider: str
    model: str
    location: str
    in_boundary: bool
    pseudonymise: bool
    approval_ref: str
    description: str

    def public(self) -> dict[str, Any]:
        return asdict(self)


ENDPOINTS: dict[str, Endpoint] = {
    "offline": Endpoint("offline", "offline", "template-drafter@1.0.0", "in-process", True, False, "n/a",
                        "Deterministic template drafter. No model, no network. Air-gapped profile."),
    "ollama": Endpoint("ollama-local", "ollama", settings.ollama_model, settings.ollama_url, True, False,
                       "local-deployment", "Open-weight model on the deployer's own hardware (Ollama)."),
    "groq": Endpoint("groq-hosted", "groq", __import__("os").environ.get("SAR_GROQ_MODEL", "openai/gpt-oss-120b"),
                     "GroqCloud API (hosted)", False, True, "REQUIRES WRITTEN APPROVAL",
                     "Hosted open-weight model on GroqCloud. Receives pseudonymised inputs only."),
    "anthropic": Endpoint("anthropic-hosted", "anthropic", settings.anthropic_model, "Anthropic API (hosted)", False, True,
                          "REQUIRES WRITTEN APPROVAL", "Hosted Claude model. Receives pseudonymised inputs only."),
}


class ModelError(RuntimeError):
    pass


SENTENCES_SCHEMA = {
    "type": "object",
    "properties": {
        "sentences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slot": {"type": "string"},
                    "text": {"type": "string"},
                    "cites": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["slot", "text", "cites"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["sentences"],
    "additionalProperties": False,
}


def available(provider: str) -> tuple[bool, str]:
    if provider == "offline":
        return True, "ready"
    if provider == "anthropic":
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False, "anthropic package not installed"
        import os
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            return False, "ANTHROPIC_API_KEY not set"
        return True, "ready"
    if provider == "groq":
        import os
        return (True, "ready") if os.environ.get("GROQ_API_KEY") else (False, "GROQ_API_KEY not set")
    if provider == "ollama":
        try:
            httpx.get(f"{settings.ollama_url}/api/tags", timeout=1.5).raise_for_status()
            return True, "ready"
        except Exception:
            return False, f"Ollama not reachable at {settings.ollama_url}"
    return False, "unknown provider"


def complete_json(provider: str, system: str, user: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Call the model; return (parsed JSON, usage/latency metadata)."""
    t0 = time.monotonic()
    if provider == "anthropic":
        import anthropic

        client = anthropic.Anthropic()
        try:
            resp = client.beta.messages.create(
                model=settings.anthropic_model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": SENTENCES_SCHEMA}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.APIConnectionError as e:
            raise ModelError(f"Cannot reach the Anthropic API: {e}") from e
        except anthropic.RateLimitError as e:
            raise ModelError("Anthropic API rate limit reached; retry shortly") from e
        except anthropic.APIStatusError as e:
            raise ModelError(f"Anthropic API error {e.status_code}: {e.message}") from e
        if resp.stop_reason == "refusal":
            raise ModelError("The model declined the request")
        if resp.stop_reason == "max_tokens":
            raise ModelError("Model output was truncated (max_tokens)")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        meta = {"tokens_in": resp.usage.input_tokens, "tokens_out": resp.usage.output_tokens, "served_by": resp.model,
                **_request_record({"model": settings.anthropic_model, "max_tokens": 16000, "system": system,
                                   "messages": [{"role": "user", "content": user}], "output_format": "json_schema"})}
    elif provider == "ollama":
        req = {"model": settings.ollama_model, "stream": False, "format": SENTENCES_SCHEMA, "options": {"temperature": 0},
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        try:
            r = httpx.post(f"{settings.ollama_url}/api/chat", timeout=600, json=req)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise ModelError(f"Ollama call failed: {e}") from e
        body = r.json()
        text = body["message"]["content"]
        meta = {"tokens_in": body.get("prompt_eval_count"), "tokens_out": body.get("eval_count"), "served_by": settings.ollama_model,
                **_request_record(req)}
    elif provider == "groq":
        text, meta = _groq(system, user)
    else:
        raise ModelError(f"Provider {provider} does not call a model")
    meta["latency_ms"] = int((time.monotonic() - t0) * 1000)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise ModelError("Model returned invalid JSON") from e
    if isinstance(parsed, list):  # some models return the sentence list without its wrapper object
        parsed = {"sentences": parsed}
    return parsed, meta


SENTENCES_HINT = ('\nReturn a JSON object of the form {"sentences": [{"slot": "<slot id>", "text": "<one sentence>", '
                  '"cites": ["<fact or hypothesis id>", ...]}, ...]}.')


def _request_record(body: dict[str, Any]) -> dict[str, Any]:
    """What an auditor needs to identify a model call (A3): the hash of the exact request sent, and its parameters."""
    return {"request_hash": chain.content_hash(body), "params": {k: v for k, v in body.items() if k not in ("messages", "system")}}


def _groq(system: str, user: str) -> tuple[str, dict[str, Any]]:
    """GroqCloud chat completions (OpenAI-compatible), JSON mode, temperature 0, with rate-limit retries."""
    import os

    model = ENDPOINTS["groq"].model
    if "OUTLINE" in user and '"sentences"' not in system:
        system = system + SENTENCES_HINT
    body: dict[str, Any] = {"model": model, "temperature": 0, "max_completion_tokens": 4000,
                            "response_format": {"type": "json_object"},
                            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if "gpt-oss" in model:
        body["reasoning_effort"] = "low"
    if "qwen" in model:
        body["reasoning_format"] = "hidden"
        body["reasoning_effort"] = "none"
    headers = {"Authorization": "Bearer " + os.environ["GROQ_API_KEY"]}
    last, failures = "", 0
    while failures < 12:  # rate-limit waits are not failures; network and server errors are
        try:
            r = httpx.post("https://api.groq.com/openai/v1/chat/completions", json=body, headers=headers, timeout=300)
        except httpx.HTTPError as e:
            failures += 1
            time.sleep(10)
            last = str(e)
            continue
        if r.status_code == 429:
            if "per day" in r.text or "TPD" in r.text:
                raise ModelError("GROQ_DAILY_LIMIT: " + r.text[:200])
            time.sleep(float(r.headers.get("retry-after") or 20) + 1)
            continue
        if r.status_code >= 500:
            failures += 1
            time.sleep(10)
            last = r.text[:200]
            continue
        if r.status_code != 200:
            raise ModelError(f"Groq API error {r.status_code}: {r.text[:300]}")
        d = r.json()
        u = d.get("usage", {})
        return d["choices"][0]["message"]["content"], {"tokens_in": u.get("prompt_tokens"), "tokens_out": u.get("completion_tokens"),
                                                        "served_by": d.get("model", model), **_request_record(body)}
    raise ModelError(f"Groq API unavailable after retries: {last}")
