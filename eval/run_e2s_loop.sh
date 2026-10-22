#!/bin/bash
# Run the held-out semantic evaluation until complete; on the judge's daily limit (exit 3) wait 30 min and resume from cache.
cd "$(dirname "$0")"
while true; do
  SAR_SEMANTIC_MODEL=qwen/qwen3.8-27b ../.venv/bin/python e2s_heldout.py >> results/e2s_v2.log 2>&1
  [ $? -eq 0 ] && break
  sleep 1800
done
