#!/usr/bin/env python3
"""resolve_user_id.py — look up the numeric X user id for a handle.

The listener requires `x_user_id` in config.json and deliberately refuses to resolve
it at startup, because an implicit lookup costs a billed API call on every boot and
an empty/incorrect id makes every delivery fail the author check.

Run this once while installing:

    ./venv/bin/python tools/resolve_user_id.py --handle example_user

Cost: `GET /2/users/by/username` is a *user* lookup, not a post read — there is no
timeline pagination and only one user is returned. Paste the printed id into
config.json as `"x_user_id": "<id>"`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import mirror as m  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Resolve an X handle to its numeric user id.")
    ap.add_argument("--handle", help="X handle, with or without @ (default: config x_user_handle)")
    ap.add_argument("--config", default=str(REPO / "config.json"))
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    cfg = m.load_config(Path(args.config))
    handle = (args.handle or cfg.get("x_user_handle") or "").strip().lstrip("@")
    if not handle:
        print("No handle: pass --handle or set x_user_handle in config.json", file=sys.stderr)
        return 1

    env_dir = m.resolve_dir(cfg.get("env_dir"), REPO)
    bearer = (m.load_env_file(env_dir / ".env").get("X_BEARER_TOKEN") or "").strip()
    if not bearer:
        print("X_BEARER_TOKEN missing from .env — add the app-only Bearer Token first.", file=sys.stderr)
        return 1

    try:
        import requests
    except ImportError:
        print("requests not installed — run: pip install -r requirements.txt", file=sys.stderr)
        return 1

    try:
        resp = requests.get(
            f"https://api.x.com/2/users/by/username/{handle}",
            params={"user.fields": "id,name,username"},
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=20,
        )
    except requests.RequestException as exc:
        print(f"request failed: {exc}", file=sys.stderr)
        return 1

    if resp.status_code != 200:
        print(f"lookup failed: HTTP {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
        return 1
    body = resp.json().get("data") or {}
    user_id = str(body.get("id") or "")
    if not user_id:
        print(f"no user in response: {resp.text[:300]}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"x_user_handle": handle, "x_user_id": user_id}, indent=2))
    else:
        print(f"@{body.get('username') or handle} -> x_user_id = {user_id}")
        print('Set it in config.json:  "x_user_id": "%s"' % user_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
