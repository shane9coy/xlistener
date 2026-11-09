#!/usr/bin/env python3
"""refresh_threads_token.py — keep the Threads long-lived token alive.

Meta's long-lived Threads tokens last 60 days and can be refreshed only while still
valid (and at least 24h old). An expired token makes every mirror attempt fail with
`[ERR post]`, which looks exactly like a webhook outage — so refresh on a schedule,
well inside the window.

Usage:
    ./venv/bin/python tools/refresh_threads_token.py            # refresh if possible
    ./venv/bin/python tools/refresh_threads_token.py --status   # validate only

Exit codes: 0 = healthy / refreshed, 1 = token invalid or refresh failed.
Safe to run often: a not-yet-due refresh is reported as "nothing to do".
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENV_FILE = REPO / ".env"


def load_env(path: Path) -> dict:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def save_env(path: Path, updates: dict[str, str]) -> None:
    text = path.read_text() if path.exists() else ""
    out, seen = [], set()
    for line in text.splitlines():
        m = re.match(r"^([A-Z_][A-Z0-9_]*)\s*=", line)
        if m and m.group(1) in updates:
            out.append(f"{m.group(1)}={updates[m.group(1)]}")
            seen.add(m.group(1))
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    path.write_text("\n".join(out).rstrip() + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser(description="Refresh or validate the Threads long-lived token.")
    ap.add_argument("--status", action="store_true", help="Only validate; do not refresh.")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    env = load_env(ENV_FILE)
    token = (env.get("THREADS_ACCESS_TOKEN") or "").strip()
    uid = (env.get("THREADS_USER_ID") or "").strip()
    secret = (env.get("THREADS_APP_SECRET") or "").strip()
    if not token or not uid:
        print("THREADS_ACCESS_TOKEN / THREADS_USER_ID missing from .env — run tools/threads_oauth.py",
              file=sys.stderr)
        return 1

    def validate(tok: str) -> tuple[bool, str]:
        try:
            who = get_json(f"https://graph.threads.net/v1.0/{uid}"
                           f"?fields=id,username&access_token={urllib.parse.quote(tok)}")
            return bool(who.get("id")), who.get("username", "")
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    ok, who = validate(token)
    if ok and not args.quiet:
        print(f"Token valid for @{who} (id {uid}).")
    if not ok:
        expired = "expired" in who.lower() or "session" in who.lower() or "190" in who
        print(f"Token INVALID ({who}).", file=sys.stderr)
        if expired:
            print("It looks expired — re-authorize: python tools/threads_oauth.py", file=sys.stderr)
        return 1

    if args.status:
        return 0

    if not secret:
        print("THREADS_APP_SECRET missing from .env; cannot refresh. Add it or re-run the OAuth flow.",
              file=sys.stderr)
        return 1

    try:
        resp = get_json(
            "https://graph.threads.net/refresh_access_token?"
            + urllib.parse.urlencode({
                "grant_type": "th_refresh_token",
                "access_token": token,
            })
        )
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        # A not-yet-due refresh is not a failure.
        print(f"Refresh not applied ({msg[:160]}). Token is still valid; try again later.")
        return 0

    new_token = resp.get("access_token") or ""
    if not new_token:
        print(f"Refresh returned no token: {resp}", file=sys.stderr)
        return 1
    save_env(ENV_FILE, {"THREADS_ACCESS_TOKEN": new_token})
    days = int(resp.get("expires_in", 0)) // 86400
    if not args.quiet:
        print(f"Refreshed. New lifetime ~{days} days. Written to {ENV_FILE} (chmod 600).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
