# SAR Studio evaluation

`SAR_Studio_Evaluation.ipynb` is saved with its outputs. It runs the real SAR Studio pipeline over **SAR-Bench-lite**, 180 synthetic AML cases with independently computed ground truth, and measures:

| Exp | Question |
|---|---|
| E0 | Does it use any outside source? (Every network connection is blocked and counted.) |
| E1 | Are the numbers right, and does every reference point at exactly the right source transactions? |
| E2 | Does the verifier catch injected hallucinations? (10 error types; false alarms on clean sentences) |
| E2b | With a hallucinating drafter, does any fault reach the final STR? |
| E3 | Does it decline weak or legitimate cases? |
| E4 / E4b | Are prompt-injection strings in memos and notes detected, and are they ever obeyed? (E4b uses held-out phrasings) |
| E5 | Is post-approval tampering detected? Is the output reproducible? |
| E6 | Naive LLM against SAR Studio with the same model. Runs only when `ANTHROPIC_API_KEY` or Ollama is set. |

```bash
python3 build_notebook.py                     # regenerate the notebook from source
jupyter nbconvert --to notebook --execute --inplace SAR_Studio_Evaluation.ipynb   # about 2 minutes
```

- `sarbench.py` is the case generator and independent ground truth.
- `harness.py` runs the pipeline and holds the scorers.
- `results/` holds CSVs and figures.

**Known limits:**
- The data is synthetic.
- E3 is partly by construction.
- The lexical support check misses semantic claims (0% in E2): the measured case for an NLI layer.
- E6 needs a model.
