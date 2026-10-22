"""Runtime configuration, read once from the environment.

Every setting has a safe offline default: with no environment at all the
service runs the deterministic drafter and never makes a network call, which
is the "air-gapped" profile from 06-system-architecture.md §4.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(default_factory=lambda: Path(_env("SAR_DB_PATH", str(BASE_DIR / "sar_studio.db"))))
    data_dir: Path = BASE_DIR / "data"
    templates_dir: Path = BASE_DIR / "templates"
    catalogue_path: Path = BASE_DIR / "catalogue" / "typologies.json"

    # Model gateway. "offline" = deterministic template drafter, no network.
    # "anthropic" = hosted Claude (pseudonymised inputs only).
    # "ollama" = local open-weight model over the Ollama HTTP API.
    default_provider: str = _env("SAR_LLM_PROVIDER", "offline")
    anthropic_model: str = _env("SAR_ANTHROPIC_MODEL", "claude-opus-5")
    ollama_url: str = _env("SAR_OLLAMA_URL", "http://127.0.0.1:11434")
    ollama_model: str = _env("SAR_OLLAMA_MODEL", "qwen2.5:14b-instruct")

    # Drafting behaviour.
    max_repair_loops: int = int(_env("SAR_MAX_REPAIR_LOOPS", "2"))
    abstain_confidence: float = float(_env("SAR_ABSTAIN_CONFIDENCE", "0.6"))

    session_hours: int = int(_env("SAR_SESSION_HOURS", "12"))
    tenant_id: str = _env("SAR_TENANT_ID", "demo-bank-pk")

    # Tazama connector (optional). Unset = the connector is off and the demo
    # cases are the only source. The CMS user needs the compliance-officer role.
    tazama_cms_url: str = _env("SAR_TAZAMA_CMS_URL", "")
    tazama_cms_ui_url: str = _env("SAR_TAZAMA_CMS_UI_URL", "")
    tazama_cms_user: str = _env("SAR_TAZAMA_CMS_USER", "")
    tazama_cms_password: str = _env("SAR_TAZAMA_CMS_PASSWORD", "")
    # Tazama's own Postgres (raw_history / configuration databases), read-only.
    tazama_pg_dsn: str = _env("SAR_TAZAMA_PG_DSN", "")
    tazama_history_days: int = int(_env("SAR_TAZAMA_HISTORY_DAYS", "30"))


settings = Settings()
