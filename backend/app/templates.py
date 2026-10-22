"""Jurisdiction templates (J) and the typology catalogue (T), loaded from JSON."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from app.config import settings

_JURISDICTION_FILES = {"GOAML:PK": "goaml_pk.json"}


@lru_cache
def load_template(jurisdiction: str) -> dict[str, Any]:
    name = _JURISDICTION_FILES.get(jurisdiction)
    if not name:
        raise ValueError(f"No template for jurisdiction {jurisdiction}")
    return json.loads((settings.templates_dir / name).read_text())


@lru_cache
def load_catalogue() -> dict[str, Any]:
    return json.loads(settings.catalogue_path.read_text())
