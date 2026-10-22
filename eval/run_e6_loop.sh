#!/bin/bash
# Run E6 for one model until complete; on the provider's daily limit (exit 3) wait 30 min and resume.
cd "$(dirname "$0")"
export SAR_GROQ_MODEL="$1"
while true; do
  ../.venv/bin/python e6_run.py 10 >> "results/e6_$(echo $1 | tr / _).log" 2>&1
  rc=$?
  [ $rc -eq 0 ] && break
  sleep 1800
done
