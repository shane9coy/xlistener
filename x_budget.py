#!/usr/bin/env python3
"""x_budget.py — hard spend caps for anything that READS X posts.

X bills per POST RETURNED, not per HTTP call. One paginated timeline read can
therefore drain a credit balance: a loop of 10 pages x 100 posts = 1000 posts
= $5.00 at the documented $0.005/post rate. This module is the guard that every
X-reading path must charge against before it spends anything.

Default limits (env-overridable, but hard-clamped to a ceiling so nothing can be
raised past it by a stray .env edit):

    X_MAX_POSTS_PER_CALL = 10     max_results ceiling for one call
    X_MAX_CALLS_PER_DAY  = 10     X read calls per UTC day
    X_MAX_POSTS_PER_DAY  = 10     posts returned per UTC day

Worst case at default limits: 10 posts x $0.005 = $0.05 per day.

The counters are persisted in .x-budget.json next to this file and reset on the
UTC date change. Everything else is in-process.

Usage:
    import x_budget
    budget = x_budget.get_budget(HERE)
    max_results = budget.cap_post_count(50)   # -> 10
    budget.note_call()                        # raises XBudgetExceeded if spent out
    resp = client.get_users_tweets(max_results=max_results, ...)
    budget.note_posts(len(resp.data or []))
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Absolute ceilings. Env vars can lower these numbers but never raise them.
CEILING_POSTS_PER_CALL = 100
CEILING_CALLS_PER_DAY = 50
CEILING_POSTS_PER_DAY = 100

DEFAULT_POSTS_PER_CALL = 10
DEFAULT_CALLS_PER_DAY = 10
DEFAULT_POSTS_PER_DAY = 10

STATE_FILENAME = ".x-budget.json"


class XBudgetExceeded(RuntimeError):
    """Raised instead of making an X read that would exceed the configured cap."""


def _env_int(name: str, default: int, ceiling: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value < 0:
        return default
    return min(value, ceiling)


def configured_limits() -> dict[str, int]:
    return {
        "posts_per_call": _env_int("X_MAX_POSTS_PER_CALL", DEFAULT_POSTS_PER_CALL, CEILING_POSTS_PER_CALL),
        "calls_per_day": _env_int("X_MAX_CALLS_PER_DAY", DEFAULT_CALLS_PER_DAY, CEILING_CALLS_PER_DAY),
        "posts_per_day": _env_int("X_MAX_POSTS_PER_DAY", DEFAULT_POSTS_PER_DAY, CEILING_POSTS_PER_DAY),
    }


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class XBudget:
    def __init__(
        self,
        state_path: Path | None = None,
        posts_per_call: int | None = None,
        calls_per_day: int | None = None,
        posts_per_day: int | None = None,
        logger: Any = None,
    ) -> None:
        limits = configured_limits()
        self.posts_per_call = min(
            posts_per_call if posts_per_call is not None else limits["posts_per_call"], CEILING_POSTS_PER_CALL
        )
        self.calls_per_day = min(
            calls_per_day if calls_per_day is not None else limits["calls_per_day"], CEILING_CALLS_PER_DAY
        )
        self.posts_per_day = min(
            posts_per_day if posts_per_day is not None else limits["posts_per_day"], CEILING_POSTS_PER_DAY
        )
        self.state_path = Path(state_path) if state_path else None
        self.logger = logger
        self.run_calls = 0
        self._state = self._load()

    # ---------- persistence ----------
    def _load(self) -> dict:
        today = _utc_today()
        fresh = {"date": today, "calls": 0, "posts": 0}
        if not self.state_path or not self.state_path.exists():
            return fresh
        try:
            data = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return fresh
        if not isinstance(data, dict) or data.get("date") != today:
            return fresh  # new UTC day -> budget resets
        return {
            "date": today,
            "calls": int(data.get("calls") or 0),
            "posts": int(data.get("posts") or 0),
        }

    def _save(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, indent=2) + "\n")
            tmp.replace(self.state_path)
        except OSError:
            # A failed bookkeeping write must never be the reason a read succeeds
            # unguarded; keep going with in-memory accounting.
            pass

    # ---------- accounting ----------
    def remaining(self) -> dict[str, int]:
        return {
            "calls_today": max(0, self.calls_per_day - int(self._state["calls"])),
            "posts_today": max(0, self.posts_per_day - int(self._state["posts"])),
            "calls_this_run": max(0, self.calls_per_day - self.run_calls),
        }

    def cap_post_count(self, requested: int) -> int:
        """Clamp a requested max_results to the per-call cap (and to at least 1)."""
        try:
            requested = int(requested)
        except (TypeError, ValueError):
            requested = self.posts_per_call
        requested = min(requested, self.posts_per_call)
        # Never ask for more posts than the day budget has left.
        left = int(self._state["posts"]) + requested
        if left > self.posts_per_day:
            requested = max(1, self.posts_per_day - int(self._state["posts"]))
        return max(1, requested)

    def note_call(self) -> None:
        """Record one X read call. Raises BEFORE the call when the cap is spent."""
        remaining = self.remaining()
        if remaining["posts_today"] <= 0:
            raise XBudgetExceeded(
                f"X read blocked: daily post budget spent "
                f"({self._state['posts']}/{self.posts_per_day} posts today). "
                f"Raise X_MAX_POSTS_PER_DAY deliberately, or wait for the UTC reset."
            )
        if remaining["calls_today"] <= 0:
            raise XBudgetExceeded(
                f"X read blocked: daily call budget spent "
                f"({self._state['calls']}/{self.calls_per_day} calls today)."
            )
        self._state["calls"] = int(self._state["calls"]) + 1
        self.run_calls += 1
        self._save()
        if self.logger:
            self.logger.info(
                f"[X-BUDGET] call {self._state['calls']}/{self.calls_per_day} today "
                f"(posts today {self._state['posts']}/{self.posts_per_day})"
            )

    def note_posts(self, count: int) -> None:
        """Record how many posts the call actually returned."""
        try:
            count = max(0, int(count))
        except (TypeError, ValueError):
            count = 0
        self._state["posts"] = int(self._state["posts"]) + count
        self._save()
        if self.logger and count:
            self.logger.info(
                f"[X-BUDGET] {count} post(s) billed; posts today "
                f"{self._state['posts']}/{self.posts_per_day} (~${0.005 * self._state['posts']:.3f})"
            )

    def summary(self) -> str:
        return (
            f"X-BUDGET: {self._state['calls']}/{self.calls_per_day} calls, "
            f"{self._state['posts']}/{self.posts_per_day} posts today "
            f"(cap {self.posts_per_call}/call, UTC date {self._state['date']})"
        )


_instances: dict[str, XBudget] = {}


def get_budget(state_dir: Path, logger: Any = None) -> XBudget:
    """Return the process-wide budget for a state directory."""
    key = str(Path(state_dir).resolve())
    if key not in _instances:
        _instances[key] = XBudget(state_path=Path(state_dir) / STATE_FILENAME, logger=logger)
    elif logger is not None and _instances[key].logger is None:
        _instances[key].logger = logger
    return _instances[key]


def reset_for_tests() -> None:
    _instances.clear()


if __name__ == "__main__":
    import sys as _sys

    _dir = Path(_sys.argv[1]) if len(_sys.argv) > 1 else Path(__file__).resolve().parent
    _b = XBudget(state_path=Path(_dir) / STATE_FILENAME)
    print(_b.summary())
    print(f"limits: {_b.posts_per_call}/call, {_b.calls_per_day} calls/day, {_b.posts_per_day} posts/day")
    print(f"remaining: {_b.remaining()}")
    print(f"worst-case cost today if fully spent: ${_b.posts_per_day * 0.005:.3f}")
