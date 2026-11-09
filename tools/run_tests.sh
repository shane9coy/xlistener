#!/usr/bin/env bash
# Run every test suite. They are all offline: no credentials, no network,
# no X or Meta API calls. Exit code 0 means everything passed.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
if [ -x venv/bin/python ]; then
  PY=venv/bin/python
fi

echo "interpreter: $PY"
"$PY" -m py_compile webhook_listener.py mirror.py x_budget.py threads_poster.py tools/*.py tests/*.py

status=0
for f in tests/*.py; do
  printf '\n== %s\n' "$f"
  "$PY" "$f" || status=1
done

if [ "$status" -eq 0 ]; then
  echo
  echo "ALL SUITES PASSED"
else
  echo
  echo "FAILURES PRESENT"
fi
exit "$status"
