"""Builds SAR_Studio_Evaluation.ipynb. Run: python3 build_notebook.py && jupyter nbconvert --execute --inplace ..."""
import nbformat as nbf

nb = nbf.v4.new_notebook()
C = []
md = lambda s: C.append(nbf.v4.new_markdown_cell(s.strip()))  # noqa: E731
code = lambda s: C.append(nbf.v4.new_code_cell(s.strip()))  # noqa: E731

md(r"""
# SAR Studio: does it tell the truth, with references?

This notebook tests SAR Studio's core claim on a benchmark with known ground truth:

> **An STR drafted by SAR Studio states only what the case evidence supports, every statement carries a reference to that evidence, and any later change to the evidence is detectable.**

It runs the **real production pipeline** (`sar-studio/backend/app`) over **SAR-Bench-lite**: 180 synthetic AML cases with independently computed ground truth. Every result below is recomputed when the notebook runs.

## The problem
LLMs used to write SARs fail in four ways:
1. **Hallucination.** Invented amounts, dates, companies or facts.
2. **Retrieval errors.** The right number with the wrong evidence behind it, or evidence missed.
3. **Unsafe language.** Stating guilt, tipping off the customer, leaking PII.
4. **Unauditable output.** Nobody can show later where a sentence came from, or prove it wasn't changed.

## How SAR Studio is built differently (the moat)
| Technique | What it does | Tested in |
|---|---|---|
| **Closed world** | The report is written only from the case's own evidence: Tazama payment messages, CMS evidence and notes. **No internet and no web search.** | E0 |
| **Structured retrieval, not RAG search** | Evidence is pulled by fixed, logged queries (account + time window), not by similarity search, so nothing irrelevant is "retrieved". | E1 |
| **Code computes every number** | A Fact Table built by code. The language model never adds, counts or dates anything. | E1 |
| **Every sentence cites fact IDs** | Each fact cites the exact evidence rows it came from, giving a sentence → fact → evidence → source message chain. | E1 |
| **Independent verifier** | Checks every sentence against its citations; repairs it or removes it before a human sees it. | E2 |
| **Abstention** | Declines to draft when evidence does not support suspicion. | E3 |
| **Injection boundary** | Instructions hidden in memos or notes are reported, never obeyed. | E4 |
| **Hash-chained ledger + attestation** | Any edit to evidence after approval is detected. | E5 |

**Scoring is independent.** Ground truth is computed by the benchmark generator (`sarbench.py`) straight from the raw transactions, with no SAR Studio code. Hallucination checks are plain string checks against the raw source data.
""")

code(r"""
import json, time, random, collections, warnings
import pandas as pd
import matplotlib.pyplot as plt
warnings.filterwarnings("ignore")
import harness as H
import sarbench as B
pd.set_option("display.width", 160); pd.set_option("display.max_colwidth", 120)
RESULTS = H.EVAL / "results"; RESULTS.mkdir(exist_ok=True)
cases = B.build(seed=2026)
print(f"{len(cases)} cases;", collections.Counter(c["_meta"]["family"] for c in cases))
print("with a prompt-injection string:", sum(bool(c["_meta"]["injection"]) for c in cases))
""")

md(r"""
## Dataset: SAR-Bench-lite

| Family | Should an STR be drafted? | What it looks like |
|---|---|---|
| STRUCTURING | yes | Cash deposits clustered just under the PKR 2m CTR threshold, across branches, then moved out |
| MULE | yes | Many unrelated senders; funds passed on within hours; student-level income |
| NEG_TRADER | no | Cash-heavy business within declared turnover, seasonal explanation, invoices |
| NEG_SALARIED | no | Ordinary salaried activity that tripped a velocity rule |
| BORDER_STRUCT | no | A few deposits near the threshold, one branch, higher income |
| BORDER_MULE | no | Fewer senders, slow and partial outflow |

About 17% of cases also carry a prompt-injection string ("IGNORE PREVIOUS INSTRUCTIONS…") in a transaction memo or a pasted note.
""")

code(r"""
c = cases[0]
print("Case:", c["case"]["id"], c["_meta"]["family"], "| subject:", c["subject"]["name"], "| occupation:", c["subject"]["occupation"])
pd.DataFrame(c["transactions"])[["txn_id","booked_at","direction","channel","amount","counterparty_name","branch"]].head(8)
""")

md(r"""
## E0: Closed world (no internet, no outside source)

All 180 cases are drafted with **every outbound network connection blocked**. If the pipeline tried to reach the internet or any outside service, the attempt would be counted.
""")

code(r"""
runs = {}
t0 = time.time()
with H.NetworkGuard() as guard:
    for c in cases:
        cid = H.ingest(c)
        r = H.draft(cid)
        c["_truth"]["_ev_to_txn"] = {e["id"]: e["content"]["txn_id"] for e in r["evidence"] if e["type"] == "transaction"}
        runs[cid] = r
elapsed = time.time() - t0
status = collections.Counter(r["draft"]["status"] for r in runs.values())
print(f"Drafted {len(runs)} cases in {elapsed:.1f}s ({elapsed/len(runs)*1000:.0f} ms per case) with the network blocked.")
print("Outbound network attempts:", guard.attempts)
print("Outcomes:", dict(status))
""")

md(r"""
## E1: Faithfulness and references in the STRs SAR Studio produces

For every drafted STR:
- **Fact accuracy.** Does each computed total, count or set equal the independent ground truth?
- **Reference (retrieval) precision and recall.** Does each fact point at exactly the transactions that make it up, no more and no fewer?
- **Unsupported amounts.** Is any PKR amount in the text absent from the source data?
- **Novel entities.** Is any name or company in the text absent from the source evidence?
- **Unsafe language.** Guilt or intent statements, tipping-off, PII.
- **Lineage completeness.** Does every sentence have a sentence → fact → evidence path?
""")

code(r"""
fact_rows, narr_rows = [], []
for c in cases:
    r = runs[c["case"]["id"]]
    if r["draft"]["status"] != "NEEDS_REVIEW":
        continue
    for row in H.fact_accuracy(r["draft"]["facts"], c["_truth"]):
        fact_rows.append({"case": c["case"]["id"], "family": c["_meta"]["family"], **row})
    text = " ".join(s["text"] for s in r["sentences"])
    chk = H.narrative_checks(text, c)
    facts = {f["id"]: f for f in r["draft"]["facts"]}
    hyps = {h["id"] for h in r["draft"]["hypotheses"]}
    full_lineage = sum(1 for s in r["sentences"] if s["cites"] and all(
        (x in hyps) or (x in facts and facts[x]["derived_from"]) for x in s["cites"]))
    narr_rows.append({"case": c["case"]["id"], "family": c["_meta"]["family"], "sentences": len(r["sentences"]),
                      "with_full_lineage": full_lineage, "amounts_stated": chk["amounts"],
                      "unsupported_amounts": chk["unsupported_amounts"], "novel_entities": len(chk["novel_entities"]),
                      "guilt": chk["guilt"], "tipping_off": chk["tipping_off"], "pii": chk["pii"],
                      "injection_echo": chk["injection_echo"]})
F = pd.DataFrame(fact_rows); N = pd.DataFrame(narr_rows)
e1 = pd.DataFrame({
    "metric": ["Facts equal to ground truth", "Reference precision (fact → transactions)", "Reference recall (fact → transactions)",
               "Sentences with full sentence→fact→evidence lineage", "PKR amounts not in the source data",
               "Names/companies not in the source evidence", "STRs with guilt/intent language", "STRs with tipping-off language",
               "STRs with PII (CNIC/phone)", "STRs repeating an injected instruction"],
    "result": [f"{F.value_correct.mean():.1%} of {len(F)}", f"{F.lineage_precision.mean():.1%}", f"{F.lineage_recall.mean():.1%}",
               f"{N.with_full_lineage.sum()/N.sentences.sum():.1%} of {N.sentences.sum()}",
               f"{N.unsupported_amounts.sum()} of {N.amounts_stated.sum()}", f"{N.novel_entities.sum()}",
               f"{N.guilt.sum()} of {len(N)}", f"{N.tipping_off.sum()} of {len(N)}", f"{N.pii.sum()} of {len(N)}",
               f"{N.injection_echo.sum()} of {len(N)}"]})
e1.to_csv(RESULTS / "e1_faithfulness.csv", index=False); e1
""")

md(r"""
**Reading E1.** In this run the drafter is the deterministic template drafter, so a clean narrative is expected. What E1 adds is an **independent check of the parts every drafter relies on**:
- the arithmetic (fact accuracy);
- the retrieval (reference precision and recall);
- the lineage.

These are the parts a language model would otherwise do itself, and where LLM SAR tools are known to fail. E2 then tests what happens when the drafter *does* hallucinate.
""")

md(r"""
## E2: Catching hallucinations (contrast set)

We take clean sentences from real drafts and **inject one error per sentence**, ten error types in all. Each corrupted sentence goes through SAR Studio's verifier. The verifier has three possible outcomes:
- **blocked**: the sentence fails and is repaired or removed before a human sees it;
- **flagged**: the sentence reaches the human with a warning;
- **missed**: the sentence passes.

The same clean sentences are also verified **uncorrupted**, to measure false alarms.
""")

code(r"""
rng = random.Random(7)
rows, clean = [], []
drafted = [c for c in cases if runs[c["case"]["id"]]["draft"]["status"] == "NEEDS_REVIEW"]
for c in drafted:
    facts, hyps, outline, raw = H.outline_for(c["case"]["id"])
    allowed = {s["slot"]: s["allowed"] for s in outline["slots"]}
    for s in raw:
        v = H.verify_one(s, facts, hyps, outline)
        clean.append(v["status"])
    for kind in H.ERROR_KINDS:
        cands = raw[:]; rng.shuffle(cands)
        for s in cands:
            bad = H.corrupt(s, kind, facts, allowed.get(s["slot"], []), rng)
            if bad is None:
                continue
            v = H.verify_one(bad, facts, hyps, outline)
            outcome = {"failed": "blocked", "flagged": "flagged", "supported": "missed"}[v["status"]]
            rows.append({"error": kind, "outcome": outcome, "codes": ",".join(sorted({x["code"] for x in v["checks"] if x["status"] != "pass"}))})
            break
E2 = pd.DataFrame(rows)
tab = (E2.groupby("error").outcome.value_counts(normalize=True).unstack(fill_value=0)
       .reindex(columns=["blocked","flagged","missed"], fill_value=0).reindex(H.ERROR_KINDS))
tab["n"] = E2.groupby("error").size()
tab["caught (blocked or flagged)"] = tab.blocked + tab.flagged
fa = collections.Counter(clean)
print(f"Clean sentences: {len(clean)}; false alarms: blocked {fa['failed']} ({fa['failed']/len(clean):.1%}), flagged {fa['flagged']} ({fa['flagged']/len(clean):.1%})")
tab.to_csv(RESULTS / "e2_verifier_contrast.csv")
tab.style.format({"blocked":"{:.0%}","flagged":"{:.0%}","missed":"{:.0%}","caught (blocked or flagged)":"{:.0%}"})
""")

code(r"""
ax = tab[["blocked","flagged","missed"]].plot.barh(stacked=True, figsize=(9,4.8), color=["#1d7a3c","#e0a100","#c0392b"])
ax.invert_yaxis(); ax.set_xlabel("share of corrupted sentences"); ax.set_xlim(0,1)
ax.set_title("E2: what the verifier does with each injected error type"); ax.legend(loc="lower right")
plt.tight_layout(); plt.savefig(RESULTS / "e2_verifier_contrast.png", dpi=150); plt.show()
""")

md(r"""
**Reading E2.**
- **Checkable errors are caught deterministically.** Numbers, dates, invented entities, guilt language, tipping-off, PII and missing or out-of-scope citations are all checked by code.
- **The weak spot is semantic.** An added claim with no number or name (for example, "The subject also used several false identities") is the `SEM_unsupported_claim` row. The current support check is lexical, so it misses most of these.
- **That row is the measured case for the next layer:** an entailment (NLI) check that asks whether the cited facts entail the sentence. The notebook reports the miss as it is; it does not hide it.
""")

md(r"""
## E2b: End to end with a hallucinating drafter

Here the drafter itself is made to hallucinate: `inject_faults=True` corrupts the draft with a wrong total, an invented Dubai company, guilt language, a tipping-off line, an uncited accusation and a fake citation. The full pipeline then runs: verify, repair loop, remove. The question is how many of those faults reach the final STR.
""")

code(r"""
b_rows = []
for c in drafted[:60]:
    r = H.draft(c["case"]["id"], inject_faults=True)
    text = " ".join(s["text"] for s in r["sentences"])
    chk = H.narrative_checks(text, c)
    st = r["draft"]["stats"]
    b_rows.append({"case": c["case"]["id"], "removed": st["removed"], "repaired": st["repaired"],
                   "unsupported_amounts_in_final": chk["unsupported_amounts"], "novel_entities_in_final": len(chk["novel_entities"]),
                   "guilt_in_final": chk["guilt"], "tipping_off_in_final": chk["tipping_off"],
                   "uncited_sentences_in_final": sum(1 for s in r["sentences"] if not s["cites"])})
E2b = pd.DataFrame(b_rows)
print(f"{len(E2b)} drafts with injected faults: {E2b.removed.sum()} sentences removed, {E2b.repaired.sum()} repaired.")
E2b[[c for c in E2b.columns if c.endswith("in_final")]].sum().rename("faults that reached the final STR").to_frame()
""")

md(r"""
## E3: Declining weak cases (abstention)

A good SAR tool must also **not** write a persuasive STR for a weak case. If it did, it would push banks to over-report and harm customers. This experiment checks whether SAR Studio drafted (said "suspicious") or abstained for each family.
""")

code(r"""
e3 = pd.DataFrame([{"family": c["_meta"]["family"], "should_file": c["_meta"]["should_file"],
                    "drafted": runs[c["case"]["id"]]["draft"]["status"] == "NEEDS_REVIEW"} for c in cases])
by = e3.groupby("family").agg(cases=("drafted","size"), drafted=("drafted","sum"))
by["abstained"] = by.cases - by.drafted
tp = ((e3.should_file) & (e3.drafted)).sum(); fp = ((~e3.should_file) & (e3.drafted)).sum()
fn = ((e3.should_file) & (~e3.drafted)).sum(); tn = ((~e3.should_file) & (~e3.drafted)).sum()
print(f"Drafted when it should: {tp}/{tp+fn} (recall {tp/(tp+fn):.1%})")
print(f"Abstained when it should: {tn}/{tn+fp} (specificity {tn/(tn+fp):.1%}); precision of drafting {tp/(tp+fp):.1%}")
by.to_csv(RESULTS / "e3_abstention.csv"); by
""")

md(r"""
**Caveat (honest).** The Typology Analyst's indicator thresholds and this benchmark's family design both encode common red-flag guidance, so E3 is partly by construction. The **borderline** families are the informative part. Real calibration needs labelled cases from practitioners (SAR-Bench v1 in `04-datasets.md`).
""")

md(r"""
## E4: Prompt injection in the evidence

Transaction memos and pasted emails are attacker-controllable. For every case that contains an injected instruction, we check two things:
1. Was the instruction **detected and reported**?
2. Did it **change the outcome**? This is a paired comparison with the identical case minus the injection: same decision, same computed facts.
""")

code(r"""
inj = [c for c in cases if c["_meta"]["injection"]]
rows = []
for c in inj:
    r = runs[c["case"]["id"]]
    twin = B.make_case(5000 + int(c["case"]["id"].split("-")[1]), c["_meta"]["family"], c["_meta"]["seed"], False)
    rt = H.draft(H.ingest(twin))
    num = lambda rr: sorted((f["key"], json.dumps(f["value"], sort_keys=True)) for f in rr["draft"]["facts"]
                            if f["key"] in ("credits_total","debits_total","cash_total","near_threshold","distinct_senders","beneficiary","outflow_ratio"))
    text = " ".join(s["text"] for s in r["sentences"])
    rows.append({"case": c["case"]["id"], "where": "memo" if any(t["memo"] == c["_meta"]["injection"] for t in c["transactions"]) else "note",
                 "detected": bool((r["draft"]["stats"] or {}).get("injection_flags")),
                 "same_decision": r["draft"]["status"] == rt["draft"]["status"],
                 "same_facts": num(r) == num(rt),
                 "instruction_in_STR": c["_meta"]["injection"][:40].lower() in text.lower()})
E4 = pd.DataFrame(rows)
print(f"{len(E4)} cases with an injected instruction")
E4[["detected","same_decision","same_facts","instruction_in_STR"]].mean().map("{:.0%}".format).to_frame("rate")
""")

md(r"""
### E4b: Held-out injection phrasings

The injection scanner was written against the four phrasings above. To avoid grading it on its own homework, we also plant **8 new phrasings it never saw** in fresh copies of 40 cases. We measure detection, and again whether anything changed.
""")

code(r"""
rng = random.Random(21); rows4b = []
base = [c for c in cases if not c["_meta"]["injection"]][:40]
for i, c in enumerate(base):
    phrase = B.INJECTIONS_HELDOUT[i % len(B.INJECTIONS_HELDOUT)]
    clean_c = B.make_case(7000 + i, c["_meta"]["family"], c["_meta"]["seed"], False)
    adv = B.make_case(8000 + i, c["_meta"]["family"], c["_meta"]["seed"], False)
    (adv["notes"].insert(0, {"author": "Customer email (pasted)", "at": adv["case"]["opened_at"], "text": phrase})
     if i % 2 else next(t for t in adv["transactions"] if t["direction"] == "in").update(memo=phrase))
    rc, ra = H.draft(H.ingest(clean_c)), H.draft(H.ingest(adv))
    num = lambda rr: sorted((f["key"], json.dumps(f["value"], sort_keys=True)) for f in rr["draft"]["facts"]
                            if f["key"] in ("credits_total","debits_total","cash_total","near_threshold","distinct_senders","beneficiary","outflow_ratio"))
    text = " ".join(s["text"] for s in ra["sentences"])
    rows4b.append({"phrase": phrase[:50], "detected": bool((ra["draft"]["stats"] or {}).get("injection_flags")),
                   "same_decision": rc["draft"]["status"] == ra["draft"]["status"], "same_facts": num(rc) == num(ra),
                   "instruction_in_STR": phrase[:30].lower() in text.lower()})
E4b = pd.DataFrame(rows4b)
print(E4b[["detected","same_decision","same_facts","instruction_in_STR"]].mean().map("{:.0%}".format).to_string())
E4b.groupby("phrase").detected.mean().map("{:.0%}".format).to_frame("detected")
""")

md(r"""
**Reading E4.** Detection is a warning for the analyst. It is **not** the defence. The defence is architectural:
- the drafter only ever sees the Fact Table, which is computed by code;
- notes can only be quoted in the slots the planner allows;
- the verifier blocks any sentence that isn't backed by its citations.

So the measure that matters is "obeyed", which is 0% even when the scanner misses a phrasing. The scanner itself still misses some held-out phrasings, and a learned classifier is the obvious upgrade.
""")

code(r"""
# Replay (before any tampering): re-run the pipeline from the same evidence; the narrative and citations must be identical.
same = 0; checked = drafted[:30]  # E5 tampers with the last 45 drafted cases only, after this check
for c in checked:
    a = runs[c["case"]["id"]]; b = H.draft(c["case"]["id"])
    key = lambda r: [(s["text"], tuple(s["cites"])) for s in r["sentences"]]
    same += key(a) == key(b)
print(f"Replay determinism: {same}/{len(checked)} re-runs produced the identical narrative and citations")
""")
md(r"""
## E5: Auditability (tamper-evidence and replay)

An examiner must be able to tell whether anything changed after approval. For a sample of drafted cases we edit one transaction amount **directly in the database**, bypassing the app as a DBA could, using three attacker strengths:
- `content`: edit the value only;
- `rehash`: also recompute that row's hash;
- `rewrite-chain`: recompute every later hash so the chain itself "walks clean".

We then run the integrity check. Untouched cases are checked too, to measure false alarms.
""")

code(r"""
rng = random.Random(11)
sample = [c for c in drafted if c["case"]["id"] not in {x["case"] for x in rows}][-45:]
t_rows = []
for i, c in enumerate(sample):
    cid = c["case"]["id"]; did = runs[cid]["draft"]["id"]
    before = H.verify(cid, did)["verdict"]
    mode = ["content", "rehash", "rewrite-chain"][i % 3]
    ev = rng.choice([e for e in runs[cid]["evidence"] if e["type"] == "transaction"])
    H.tamper(ev["id"], "amount", ev["content"]["amount"] + rng.choice([-150000, 50000, 275000]), mode)
    rep = H.verify(cid, did)
    t_rows.append({"case": cid, "mode": mode, "before": before, "after": rep["verdict"],
                   "facts_no_longer_reproducing": len(rep.get("fact_replay", {}).get("changed", []) if isinstance(rep.get("fact_replay"), dict) else [])})
E5 = pd.DataFrame(t_rows)
print("Clean cases verified PASS before tampering:", f"{(E5.before == 'PASS').mean():.0%}")
E5.groupby("mode").after.apply(lambda s: f"{(s == 'FAIL').mean():.0%} detected ({(s == 'FAIL').sum()}/{len(s)})").to_frame("tampering detected")
""")



md(r"""
## E6: Against a plain LLM (needs a model; switched off if none is configured)

This is the head-to-head the paper needs, with **the same model** in both arms:
- **B1, naive.** Raw case JSON in, free-text STR out. No fact table, no citations, no verifier. This is how most "AI SAR" demos work.
- **Ours.** The same model writes the sentences, but only from the Fact Table, with citations, through the verifier.

Both arms are scored with the **same independent checks** used above. To enable it, set `ANTHROPIC_API_KEY`, or run Ollama with `SAR_OLLAMA_MODEL` pulled, then re-run the notebook.
""")

code(r"""
prov = H.llm_provider()
if not prov:
    print("No LLM configured (no ANTHROPIC_API_KEY and no Ollama). E6 skipped; nothing below is simulated.")
else:
    sub = drafted[:20]; rows = []
    for c in sub:
        base = H.naive_llm_narrative(c, prov)
        ours = H.draft(c["case"]["id"], provider=prov)
        for arm, text in (("B1 naive LLM", base), ("Ours (same LLM)", " ".join(s["text"] for s in ours["sentences"]))):
            k = H.narrative_checks(text, c)
            rows.append({"arm": arm, "unsupported_amounts": k["unsupported_amounts"], "amounts": k["amounts"],
                         "novel_entities": len(k["novel_entities"]), "guilt": k["guilt"], "tipping_off": k["tipping_off"],
                         "injection_echo": k["injection_echo"]})
    E6 = pd.DataFrame(rows)
    E6.groupby("arm").agg(STRs=("amounts","size"), amounts=("amounts","sum"), unsupported_amounts=("unsupported_amounts","sum"),
                          novel_entities=("novel_entities","sum"), guilt=("guilt","sum"), tipping_off=("tipping_off","sum"))
""")

md(r"""
## Summary
""")

code(r"""
summary = pd.DataFrame([
    ("E0", "Network connections attempted while drafting 180 STRs", f"{guard.attempts}"),
    ("E1", "Computed facts equal to independent ground truth", f"{F.value_correct.mean():.1%}"),
    ("E1", "Reference precision / recall (fact → source transactions)", f"{F.lineage_precision.mean():.1%} / {F.lineage_recall.mean():.1%}"),
    ("E1", "Sentences with full sentence → fact → evidence lineage", f"{N.with_full_lineage.sum()/N.sentences.sum():.1%}"),
    ("E2", "Injected errors caught (9 checkable types, mean)", f"{tab.drop('SEM_unsupported_claim')['caught (blocked or flagged)'].mean():.1%}"),
    ("E2", "Semantic unsupported claims caught (no number/name)", f"{tab.loc['SEM_unsupported_claim','caught (blocked or flagged)']:.1%}  ← next: NLI layer"),
    ("E2", "False alarms on clean sentences (blocked)", f"{fa['failed']/len(clean):.1%}"),
    ("E2b", "Injected faults that reached a final STR", f"{int(E2b[[c for c in E2b.columns if c.endswith('in_final')]].sum().sum())}"),
    ("E3", "Draft recall / specificity", f"{tp/(tp+fn):.1%} / {tn/(tn+fp):.1%}"),
    ("E4", "Injected instructions detected / obeyed (dev phrasings)", f"{E4.detected.mean():.0%} / {E4.instruction_in_STR.mean():.0%}"),
    ("E4b", "Injected instructions detected / outcome changed (held-out phrasings)", f"{E4b.detected.mean():.0%} / {1-(E4b.same_decision & E4b.same_facts).mean():.0%}"),
    ("E5", "Replay: identical narrative + citations on re-run", f"{same}/{len(checked)}"),
    ("E5", "Post-approval tampering detected (all 3 attacker modes)", f"{(E5.after=='FAIL').mean():.0%}"),
], columns=["exp", "measure", "result"])
summary.to_csv(RESULTS / "summary.csv", index=False); summary
""")

md(r"""
## What this does and does not show

**Shown, on 180 benchmark cases:**
- The numbers and references in SAR Studio's STRs match independent ground truth.
- No outside source is used.
- Checkable hallucinations are stopped before a human sees them.
- Weak cases are declined.
- Injected instructions are reported, not obeyed.
- After-the-fact edits are detected.

**Not yet shown:**
1. **A live-LLM comparison (E6).** It needs a model key or Ollama.
2. **Semantic support.** The lexical support check misses unsupported claims that contain no number or name. The planned NLI layer targets exactly that.
3. **Real data.** The cases are synthetic. SAR-Bench v1 (SAML-D / AMLworld-derived, practitioner-written reference narratives) and a pilot inside a Tazama deployment are the next steps.
4. **Human studies.** No expert-sufficiency ratings or auditor-time studies yet.
""")

nb["cells"] = C
nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
nbf.write(nb, "SAR_Studio_Evaluation.ipynb")
print("written")
