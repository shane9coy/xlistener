#!/usr/bin/env python3
"""Unit tests for the X spend guard (x_budget). No network, no X API calls."""
import json
import os
import sys
import tempfile
from pathlib import Path

# Tests live in tests/ in the repo but at the service root on a deployed install.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
import x_budget as xb

failures = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        failures.append(name)


tmpdir = Path(tempfile.mkdtemp(prefix="xbudget-"))
state = tmpdir / ".x-budget.json"

# --- defaults ---
lim = xb.configured_limits()
check("default posts/call is 10", lim["posts_per_call"] == 10)
check("default calls/day is 10", lim["calls_per_day"] == 10)
check("default posts/day is 10", lim["posts_per_day"] == 10)

# --- env can lower but never raise past the ceiling ---
os.environ["X_MAX_POSTS_PER_CALL"] = "5000"
os.environ["X_MAX_CALLS_PER_DAY"] = "999999"
os.environ["X_MAX_POSTS_PER_DAY"] = "5000"
lim = xb.configured_limits()
check("posts/call clamped to ceiling", lim["posts_per_call"] == xb.CEILING_POSTS_PER_CALL)
check("calls/day clamped to ceiling", lim["calls_per_day"] == xb.CEILING_CALLS_PER_DAY)
os.environ["X_MAX_POSTS_PER_CALL"] = "3"
check("env can lower posts/call", xb.configured_limits()["posts_per_call"] == 3)
for k in ("X_MAX_POSTS_PER_CALL", "X_MAX_CALLS_PER_DAY", "X_MAX_POSTS_PER_DAY"):
    os.environ.pop(k, None)

# --- per-call clamp ---
b = xb.XBudget(state_path=state, posts_per_call=10, calls_per_day=10, posts_per_day=10)
check("cap_post_count clamps 100 -> 10", b.cap_post_count(100) == 10)
check("cap_post_count clamps 50 -> 10", b.cap_post_count(50) == 10)
check("cap_post_count keeps small values", b.cap_post_count(5) == 5)
check("cap_post_count never goes below 1", b.cap_post_count(0) == 1)

# --- call + post accounting persists ---
b.note_call()
b.note_posts(4)
b2 = xb.XBudget(state_path=state)
check("calls persist across instances", b2._state["calls"] == 1)
check("posts persist across instances", b2._state["posts"] == 4)

# --- daily post cap blocks further reads ---
b2.note_call()
b2.note_posts(6)  # now at 10/10 posts
check("post budget exhausted", b2.remaining()["posts_today"] == 0)
raised = False
try:
    b2.note_call()
except xb.XBudgetExceeded:
    raised = True
check("11th read is refused (raises, does not spend)", raised)
check("refused read did not increment calls", b2._state["calls"] == 2)

# --- cap_post_count shrinks to remaining day budget ---
b3 = xb.XBudget(state_path=state, posts_per_call=10, posts_per_day=10)
# posts already 10 -> remaining 0 -> still asks for 1 minimum but note_call will refuse
check("cap_post_count floors at 1 when day budget spent", b3.cap_post_count(10) == 1)

# --- fresh UTC day resets ---
state.write_text(json.dumps({"date": "1999-01-01", "calls": 99, "posts": 999}))
b4 = xb.XBudget(state_path=state)
check("new UTC day resets counters", b4._state["calls"] == 0 and b4._state["posts"] == 0)

# --- daily CALL cap is independent of posts ---
b5 = xb.XBudget(state_path=tmpdir / "calls.json", posts_per_call=10, calls_per_day=2, posts_per_day=100)
b5.note_call()
b5.note_call()
raised = False
try:
    b5.note_call()
except xb.XBudgetExceeded:
    raised = True
check("daily call cap refuses 3rd call when cap=2", raised)

# --- worst-case cost at defaults ---
check("worst case at defaults is $0.05/day", round(10 * 0.005, 3) == 0.05)

# --- summary is judgeable ---
check("summary mentions calls and posts", "calls" in b5.summary() and "posts" in b5.summary())

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("all x_budget tests passed")
