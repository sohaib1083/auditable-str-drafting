"""PII Guard (agent 1): pseudonymise entity strings before any out-of-boundary call.

The mapping never leaves this process; it is used to restore real values in
the model's output. MVP scope: exact-string replacement of every entity the
Fact Builder knows (names, accounts, banks, branches, authors). A production
deployment would add an NER model for free text (06-system-architecture §2).
"""
from __future__ import annotations

import re
from typing import Any


class Pseudonymiser:
    def __init__(self, facts: list[dict[str, Any]]) -> None:
        ents: set[str] = set()
        for f in facts:
            ents.update(e for e in f.get("entities", []) if e and len(e) > 2)
        # Longest first, so "Al-Noor Exchange Company" is replaced before "Al-Noor".
        self.forward: dict[str, str] = {}
        for n, e in enumerate(sorted(ents, key=len, reverse=True), 1):
            self.forward[e] = f"[ENTITY_{n:02d}]"
        self.reverse = {v: k for k, v in self.forward.items()}

    def mask(self, text: str) -> str:
        for real, token in self.forward.items():
            text = text.replace(real, token)
        return text

    def unmask(self, text: str) -> str:
        return re.sub(r"\[ENTITY_\d{2}\]", lambda m: self.reverse.get(m.group(0), m.group(0)), text)

    @property
    def count(self) -> int:
        return len(self.forward)
