"""Write backend/release.json: the component versions of this release and the hash of its benchmark results.

Audit packs copy this manifest, so an auditor holding one report can find the benchmark results
published for the exact release that produced it (audit question A10).

Usage: python3 release_manifest.py <release version>   (run after e6_analyze.py has written paper_numbers.json)
"""
import hashlib
import json
import sys

import harness as H
from app.pipeline.drafter import PROMPT_VERSION
from app.templates import load_catalogue, load_template
from app.verification.claims import VERIFIER_VERSION

ROOT = H.EVAL.parent
BENCH = "eval/results/paper_numbers.json"
tmpl = load_template("GOAML:PK")
manifest = {
    "name": "auditable-str-drafting",
    "version": sys.argv[1],
    "components": {"verifier": VERIFIER_VERSION, "fact_builder": "facts@1.0.0", "typology_catalogue": load_catalogue()["version"],
                   "drafter_prompt": PROMPT_VERSION, "template": f"{tmpl['id']}@{tmpl['version']}", "audit_pack": "1.1"},
    "benchmark": {"name": "SAR-Bench-lite", "path": BENCH, "sha256": hashlib.sha256((ROOT / BENCH).read_bytes()).hexdigest()},
}
(H.BACKEND / "release.json").write_text(json.dumps(manifest, indent=1) + "\n")
print(json.dumps(manifest, indent=1))
