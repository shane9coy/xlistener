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

# Same gate as CI: every tool must at least import and answer --help.
for t in tools/doctor.py tools/resolve_user_id.py tools/setup_xaa.py tools/threads_oauth.py tools/refresh_threads_token.py tools/replay_backlog.py; do
  printf '\n== %s --help\n' "$t"
  "$PY" "$t" --help > /dev/null || { echo "FAILED: $t --help"; status=1; }
done

if [ "$status" -eq 0 ]; then
  echo
  echo "ALL SUITES PASSED"
else
  echo
  echo "FAILURES PRESENT"
fi
exit "$status"
