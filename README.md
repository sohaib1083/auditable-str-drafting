# Auditable LLM drafting of suspicious transaction reports

This is the code, benchmark and results for the paper *"Every claim traceable: an auditable large language model pipeline for drafting suspicious transaction reports"* (Shamsi, 2026, under review).

The pipeline drafts the narrative of a suspicious transaction report (STR) so that an auditor can check every claim:
- code computes every amount, date and count from the case evidence, and the LLM never does arithmetic;
- the LLM writes only sentences that cite those facts, taken from an allow-list set by a planner;
- layered verifiers check each sentence and repair or remove anything they cannot confirm:
  - deterministic: numbers, dates, names, citations;
  - policy: intent language, tipping-off, personal identifiers;
  - optional semantic: an LLM judge checks that the cited evidence supports the sentence;
- the pipeline declines to draft when the evidence does not support suspicion;
- an analyst ("maker") reviews and a different officer ("checker") attests;
- every step is written to a hash-chained ledger;
- the output is goAML XML plus an audit pack.

The main experiment (**E7**) tests the auditability claim from the outside. A separate auditor program (`eval/auditor.py`) shares no code with the pipeline and reads only the exported audit pack. It tries to answer the paper's ten audit questions for every report and every declined case, and it is tested against 16 kinds of tampering.

All data in this repository is **synthetic**. No real customer, account or institution data is included.

## Contents

| Path | What |
|---|---|
| `backend/app/` | The pipeline: intake, fact table, typology analysis, planner, drafter, verifiers, ledger, goAML adapter, model gateway |
| `backend/tests/` | Unit and end-to-end tests |
| `eval/sarbench.py` | **SAR-Bench-lite** generator: 180 cases in six families, with independently computed ground truth and embedded prompt-injection strings |
| `eval/harness.py` | Runs the real pipeline on the benchmark; independent scorers |
| `eval/SAR_Studio_Evaluation.ipynb` | Experiments E0-E5 (template drafter, no network), saved with outputs |
| `eval/e6_run.py`, `eval/e6_analyze.py` | E6: a naive LLM against the pipeline, using the same model, on 60 stratified cases and three open-weight models |
| `eval/e2_semantic.py`, `eval/e2s_heldout.py` | E2-S: the semantic verifier layer, with a development sample and a held-out evaluation |
| `eval/auditor.py` | E7: the independent auditor. It uses only the standard library and answers audit questions A1-A10 from one audit pack |
| `eval/e7_audit.py` | E7: runs the full review workflow, exports audit packs, audits them and applies the tampering mutations |
| `eval/release_manifest.py`, `backend/release.json` | The release manifest that audit packs carry (component versions and the hash of the published benchmark results) |
| `eval/results/` | All outputs: CSVs, per-case E6 records (narratives and scores), `paper_numbers.json`, and `e7/` (audit packs, the FIU copies of filed reports, audit results and tampering results for pack versions 1.0 and 1.1) |

## Reproduce

Requirements: Python 3.10+.

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt pandas matplotlib nbconvert ipykernel scipy
```

**E0-E5** (offline, about 2 minutes, no API key):

```bash
cd eval && ../.venv/bin/jupyter nbconvert --to notebook --execute --inplace SAR_Studio_Evaluation.ipynb
```

**E6** (needs a GroqCloud API key; cached per call and resumable):

```bash
cd eval && GROQ_API_KEY=... SAR_GROQ_MODEL=openai/gpt-oss-120b ../.venv/bin/python e6_run.py 10
../.venv/bin/python e6_analyze.py
```

**E2-S** (semantic layer):

```bash
cd eval && GROQ_API_KEY=... SAR_SEMANTIC_MODEL=qwen/qwen3.8-27b ../.venv/bin/python e2_semantic.py 200
GROQ_API_KEY=... SAR_SEMANTIC_MODEL=qwen/qwen3.8-27b ../.venv/bin/python e2s_heldout.py
```

**E7** (independent audit). The template-drafter run is offline and takes about 2 minutes; the LLM run needs a key:

```bash
cd eval && ../.venv/bin/python e7_audit.py v1.1 offline
GROQ_API_KEY=... SAR_GROQ_MODEL=openai/gpt-oss-120b ../.venv/bin/python e7_audit.py v1.1 groq 5
```

To audit a single pack yourself:

```python
import json, auditor
print(auditor.audit(json.load(open("results/e7/v1.1/offline/packs/BENCH-0001.json")))["questions"])
```

Without the `replay` and `release_root` arguments, A7 and A10 come back as unanswerable, because they need the released code and benchmark files. `e7_audit.py` shows how to supply both. The `v1.0` results were produced with the original audit pack format, before the changes E7 prompted. The current code produces version 1.1.

Models were served by GroqCloud at temperature 0. Hosted-model outputs can change over time, so the cached records in `eval/results/` are the reference for the paper's numbers.

## Licence

- Code: Apache-2.0 (see `LICENSE`).
- Benchmark cases, prompts and results: CC BY 4.0 (see `NOTICE`).

## Citation

See `CITATION.cff`.
