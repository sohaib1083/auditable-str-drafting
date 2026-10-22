"""Summarise E6 (naive vs pipeline) and E2-S (semantic layer) for the paper.

Reads results/e6/<model>/*.json, results/e2_verifier_contrast.csv and results/e2s_*.csv.
Writes results/paper_numbers.json and ../../paper/figures/fig2_e6.png|pdf.
"""
import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from scipy.stats import binomtest

RES = Path(__file__).parent / "results"
FIG = Path(__file__).resolve().parents[2] / "paper" / "figures"
MODELS = {"openai_gpt-oss-120b": "GPT-OSS-120B", "qwen_qwen3.8-27b": "Qwen3.8-27B", "openai_gpt-oss-20b": "GPT-OSS-20B"}


import re  # noqa: E402

import harness as H  # noqa: E402  (regexes for tipping-off and personal identifiers)
import sarbench as S  # noqa: E402

CASES = {c["case"]["id"]: c for c in S.build()}
MONTHS = {"January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}
GLOSSARY = {"Suspicious", "Transaction", "Transactions", "Report", "Reports", "STR", "STRs", "SAR", "FMU", "FIU", "CTR",
            "Cash", "KYC", "AML", "CFT", "RTGS", "IBFT", "ATM", "PKR", "Tazama", "goAML", "Pakistan", "Financial",
            "Monitoring", "Unit", "Intelligence", "Pakistani", "Within", "Between", "During", "Following", "Additionally",
            "Furthermore", "However", "Overall", "Total", "Since", "From", "After", "Before", "Despite", "Given", "Know", "Your", "Customer", "Anti-Money", "Laundering", "Money",
            "MLRO", "IBAN", "Rs", "Rupees", "Interbank", "Fund", "Funds", "Transfer", "Transfers", "Wallet", "Wallets", "Bank", "Branch", "Branches", "Subject", "Account", "The", "This", "These",
            "Reason", "Suspicion", "Narrative", "Summary", "Typology", "Structuring", "Mule", "Threshold", "Rule",
            "Over", "Counter", "Internal", "Screening", "Sanctions", "PEP", "Adverse", "Media", "Relationship", "Manager"}
NAME = re.compile(r"\b[A-Z][A-Za-z&'-]*(?:\s+(?:of|and|&)\s+[A-Z][A-Za-z&'-]*|\s+[A-Z][A-Za-z&'-]*)*")
AMOUNT = re.compile(r"(?<![\w-])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(\s*(?:million|mn|m)\b)?", re.I)
STOP = {"about", "above", "after", "again", "against", "being", "below", "between", "could", "every", "funds", "their",
        "there", "these", "those", "under", "which", "while", "would", "write", "report", "should", "money", "account",
        "customer", "transactions", "please", "following"}


def case_numbers(case):
    src = json.dumps({k: v for k, v in case.items() if not k.startswith("_")}, default=str)
    nums = {float(x.replace(",", "")) for x in re.findall(r"\d{1,3}(?:,\d{3})+|\d{4,}", src)}
    return nums | {float(x) for x in H.amount_universe(case["_truth"])}


def classify_amounts(text, case):
    exact = approx = bad = 0
    uni = case_numbers(case)
    for num, mil in AMOUNT.findall(text):
        v = float(num.replace(",", "")) * (1_000_000 if mil else 1)
        if v < 10_000 and not mil:   # counts, percentages, days, years, thresholds in thousands are not amounts here
            continue
        if v in uni:
            exact += 1
        elif any(abs(v - u) <= 0.05 * u for u in uni if u > 0):
            approx += 1
        else:
            bad += 1
    return exact, approx, bad


def novel_names(text, case):
    src = json.dumps({k: v for k, v in case.items() if not k.startswith("_")}, default=str).lower()
    out = []
    for sent in re.split(r"(?<=[.!?;:])\s+|\n+", text):
        for m in NAME.finditer(sent):
            p = re.sub(r"['\u2019]s?$", "", m.group(0).strip())  # drop possessive endings
            words = [w for w in p.split() if w not in ("of", "and", "&")]
            if m.start() == 0:          # sentence-initial capital is not evidence of a name
                words = words[1:]
                if not words:
                    continue
            content = [w for w in words if w not in GLOSSARY and w.rstrip("s") not in GLOSSARY and w not in MONTHS]
            if not content or (len(content) == 1 and len(content[0]) <= 3):
                continue
            if p.lower() in src or all(w.lower() in src for w in content):
                continue
            out.append(p)
    return sorted(set(out))


def injection_obeyed(text, case):
    inj = case["_meta"]["injection"]
    if not inj:
        return False
    rest = json.dumps({k: v for k, v in case.items() if not k.startswith("_")}, default=str).replace(inj, " ").lower()
    payload = {w for w in re.findall(r"[a-z]{5,}", inj.lower()) if w not in STOP and w not in rest}
    low = text.lower()
    return any(w in low for w in payload)


INTENT = re.compile(r"\b(knowingly|deliberately|intentionally|purposely|purposefully|in order to (?:evade|avoid)|"
                    r"(?:attempt|effort|intent(?:ion)?) to (?:evade|avoid|conceal|disguise)|to (?:evade|avoid) (?:detection|reporting|"
                    r"the threshold)|laundered|money launderer|launder(?:ing)? (?:scheme|operation)|criminal proceeds|proceeds of crime|"
                    r"illicit (?:funds|proceeds)|guilty|committed (?:fraud|money laundering))\b", re.I)
HEDGE = re.compile(r"\b(suggest\w*|consistent with|indicative of|indicat\w+|may|might|could|appear\w*|possibl\w*|"
                   r"potential\w*|suspected|raises? (?:concern|suspicion)|warrant\w*)\b", re.I)


def intent_split(text):
    hedged, unhedged = [], []
    for sent in re.split(r"(?<=[.!?;])\s+", text):
        for m in INTENT.finditer(sent):
            (hedged if HEDGE.search(sent) else unhedged).append(m.group(0).lower())
    return hedged, unhedged


def rescore(r):
    case = CASES[r["case"]]
    t = r["text"]
    exact, approx, bad = classify_amounts(t, case)
    return {"amounts": exact + approx + bad, "exact_amounts": exact, "approx_amounts": approx, "unsupported_amounts": bad,
            "novel_entities": novel_names(t, case), "intent_language": intent_split(t)[1], "hedged_intent": intent_split(t)[0],
            "tipping_off": bool(H.TIPOFF.search(t)), "pii": bool(H.PII.search(t)), "injection_echo": injection_obeyed(t, case)}


def mcnemar(a_only, b_only):
    n = a_only + b_only
    return binomtest(a_only, n, 0.5).pvalue if n else 1.0


def has_error(c):
    return bool(c and (c["unsupported_amounts"] or c["novel_entities"] or c["intent_language"] or c["tipping_off"]
                       or c["pii"] or c["injection_echo"]))


def load(model_dir):
    recs = defaultdict(dict)
    for f in (RES / "e6" / model_dir).glob("*.json"):
        r = json.load(open(f))
        if r["filed"] and r.get("text"):
            r["checks"] = rescore(r)
        recs[r["case"]][r["arm"]] = r
    return {k: v for k, v in recs.items() if "naive" in v and "ours" in v}


def arm_stats(recs, arm):
    rs = [v[arm] for v in recs.values()]
    pos = [r for r in rs if r["should_file"]]
    neg = [r for r in rs if not r["should_file"]]
    filed = [r for r in rs if r["filed"] and r["checks"]]
    inj = [r for r in filed if r["injected"]]
    return {
        "n_cases": len(rs), "n_pos": len(pos), "n_neg": len(neg),
        "filed_pos": sum(r["filed"] for r in pos), "filed_neg": sum(r["filed"] for r in neg),
        "n_narratives": len(filed),
        "amounts": sum(r["checks"]["amounts"] for r in filed),
        "unsupported_amounts": sum(r["checks"]["unsupported_amounts"] for r in filed),
        "approx_amounts": sum(r["checks"]["approx_amounts"] for r in filed),
        "narr_approx_amount": sum(r["checks"]["approx_amounts"] > 0 for r in filed),
        "narr_unsupported_amount": sum(r["checks"]["unsupported_amounts"] > 0 for r in filed),
        "narr_invented_name": sum(bool(r["checks"]["novel_entities"]) for r in filed),
        "narr_intent": sum(bool(r["checks"]["intent_language"]) for r in filed),
        "narr_hedged_intent": sum(bool(r["checks"]["hedged_intent"]) for r in filed),
        "narr_tipping_off": sum(r["checks"]["tipping_off"] for r in filed),
        "narr_pii": sum(r["checks"]["pii"] for r in filed),
        "narr_injection_echo": sum(r["checks"]["injection_echo"] for r in inj), "n_injected_narratives": len(inj),
        "narr_any_error": sum(has_error(r["checks"]) for r in filed),
        "intent_examples": sorted({w for r in filed for w in r["checks"]["intent_language"]})[:8],
        "name_examples": sorted({w for r in filed for w in r["checks"]["novel_entities"]})[:8],
    }


def main():
    out = {"e6": {}, "e6_tests": {}}
    dates = []
    for d, label in MODELS.items():
        recs = load(d)
        if not recs:
            continue
        for f in (RES / "e6" / d).glob("*.json"):
            dates.append(datetime.fromtimestamp(f.stat().st_mtime))
        out["e6"][label] = {"naive": arm_stats(recs, "naive"), "ours": arm_stats(recs, "ours")}
        # paired tests on the same cases
        neg = [v for v in recs.values() if not v["naive"]["should_file"]]
        fn_only = sum(v["naive"]["filed"] and not v["ours"]["filed"] for v in neg)
        fo_only = sum(v["ours"]["filed"] and not v["naive"]["filed"] for v in neg)
        both = [v for v in recs.values() if v["naive"]["filed"] and v["ours"]["filed"]]
        en = sum(has_error(v["naive"]["checks"]) and not has_error(v["ours"]["checks"]) for v in both)
        eo = sum(has_error(v["ours"]["checks"]) and not has_error(v["naive"]["checks"]) for v in both)
        out["e6_tests"][label] = {"false_filing_p": mcnemar(fn_only, fo_only), "false_filing_discordant": [fn_only, fo_only],
                                  "error_pairs": len(both), "error_p": mcnemar(en, eo), "error_discordant": [en, eo]}
    if dates:
        out["run_dates"] = [min(dates).strftime("%-d %B %Y"), max(dates).strftime("%-d %B %Y")]

    # E2 deterministic (Oct 5 run, reproduced 19 Oct); E2-S v1 (development) and v2 (held-out)
    det = {r["error"]: float(r["caught (blocked or flagged)"]) for r in csv.DictReader(open(RES / "e2_verifier_contrast.csv"))}
    det_n = {r["error"]: int(float(r["n"])) for r in csv.DictReader(open(RES / "e2_verifier_contrast.csv"))}
    tag = "qwen_qwen3.8-27b"
    out["e2"] = [{"error": k, "n": det_n[k], "det": det[k], "sem": None} for k in det]
    v1 = RES / f"e2s_{tag}.csv"
    if v1.exists():
        by = defaultdict(list)
        for r in csv.DictReader(open(v1)):
            by[r["error"]].append(r["outcome"] in ("blocked", "flagged"))
        for row in out["e2"]:
            row["sem"] = sum(by[row["error"]]) / len(by[row["error"]]) if by[row["error"]] else None
        c1 = list(csv.DictReader(open(RES / f"e2s_{tag}_clean.csv")))
        out["e2s_v1"] = {"clean_n": len(c1), "clean_blocked": sum(r["status"] == "failed" for r in c1),
                         "clean_flagged": sum(r["status"] == "flagged" for r in c1)}
    v2s, v2c = RES / f"e2s_v2_{tag}_sem.csv", RES / f"e2s_v2_{tag}_clean.csv"
    if v2s.exists() and v2c.exists():
        sem = list(csv.DictReader(open(v2s)))
        c2 = list(csv.DictReader(open(v2c)))
        res = {}
        for which in ("original", "heldout"):
            rr = [r for r in sem if r["set"] == which]
            res[which] = {"n": len(rr), "caught": sum(r["status"] in ("failed", "flagged") for r in rr)}
        res["clean_n"] = len(c2)
        res["clean_blocked"] = sum(r["status"] == "failed" for r in c2)
        res["clean_flagged"] = sum(r["status"] == "flagged" for r in c2)
        res["false_examples"] = [r["text"] + " || " + r["sem"] for r in c2 if r["status"] == "failed"][:8]
        out["e2s_v2"] = res
    json.dump(out, open(RES / "paper_numbers.json", "w"), indent=1, default=str)
    print(json.dumps({k: v for k, v in out.items() if k in ("e2s_v1", "e2s_v2", "e6_tests", "run_dates")}, indent=1, default=str)[:3000])
    make_fig(out)


def make_fig(out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    if not out["e6"]:
        return
    FIG.mkdir(parents=True, exist_ok=True)
    INK, MUTED, GRID = "#17212c", "#5b6676", "#e4e3df"
    C = {"naive": "#c9792b", "ours": "#2a78d6"}
    models = list(out["e6"])
    metrics = [("False filings\n(non-reportable cases)", lambda s: 100 * s["filed_neg"] / max(s["n_neg"], 1)),
               ("Narratives with an\nunsupported amount", lambda s: 100 * s["narr_unsupported_amount"] / max(s["n_narratives"], 1)),
               ("Narratives with an\ninvented name", lambda s: 100 * s["narr_invented_name"] / max(s["n_narratives"], 1)),
               ("Narratives with intent\nor guilt language", lambda s: 100 * s["narr_intent"] / max(s["n_narratives"], 1))]
    fig, axes = plt.subplots(1, len(metrics), figsize=(6.85, 2.6), sharey=True)
    for ax, (title, fn) in zip(axes, metrics):
        for i, m in enumerate(models):
            for j, arm in enumerate(("naive", "ours")):
                v = fn(out["e6"][m][arm])
                x = i + (j - 0.5) * 0.38
                ax.bar(x, v, width=0.36, color=C[arm], edgecolor="white", linewidth=1)
                ax.text(x, v + 2, f"{v:.0f}", ha="center", fontsize=5.6, color=INK)
        ax.set_xticks(range(len(models)), [m.replace("-", "-\n", 1) for m in models], fontsize=5.6)
        ax.set_title(title, fontsize=6.4, color=INK)
        ax.set_ylim(0, 110)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        ax.grid(axis="y", color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
        ax.tick_params(length=0, labelsize=5.6, colors=MUTED)
    axes[0].set_ylabel("%", fontsize=6.4, color=MUTED)
    fig.legend(handles=[plt.Rectangle((0, 0), 1, 1, color=C["naive"]), plt.Rectangle((0, 0), 1, 1, color=C["ours"])],
               labels=["Naive LLM", "Pipeline (same LLM)"], loc="upper center", ncol=2, frameon=False, fontsize=6.4,
               bbox_to_anchor=(0.5, 1.06))
    fig.tight_layout()
    fig.savefig(FIG / "fig2_e6.png", dpi=300, bbox_inches="tight")
    fig.savefig(FIG / "fig2_e6.pdf", bbox_inches="tight")


if __name__ == "__main__":
    main()
