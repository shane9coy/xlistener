#!/usr/bin/env python3
"""Create / inspect X Activity API (XAA, "webhook v2") subscriptions for this listener.

XAA replaces the Account Activity API (AAA EOL 2027-01-31). It reuses the SAME
webhook, CRC and signature scheme as AAA, but subscriptions are per event type and
the payload envelope is different (handled by webhook_listener.py).

This tool never posts to Threads and never touches state.db.

Usage (DRY RUN is the default — nothing is created unless you pass --apply):

    ./venv/bin/python tools/setup_xaa.py                 # show what would happen
    ./venv/bin/python tools/setup_xaa.py --apply         # create the subscriptions
    ./venv/bin/python tools/setup_xaa.py --list          # list webhooks + subscriptions

Billing: each delivered `post.create` event is billed as a Post ($0.005 at the
documented rate). Subscription creation itself is not billed. See
https://docs.x.com/x-api/getting-started/pricing
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
SERVICE_DIR = HERE.parent
sys.path.insert(0, str(SERVICE_DIR))

import mirror as m  # noqa: E402  (shared config/env plumbing)

DEFAULT_EVENT_TYPES = ["post.create"]
API = "https://api.x.com"


def load_cfg() -> dict:
    return m.load_config(SERVICE_DIR / "config.json")


def bearer_token(cfg: dict) -> str:
    env = m.load_env_file(Path(cfg.get("env_dir") or SERVICE_DIR) / ".env")
    token = (env.get("X_BEARER_TOKEN") or "").strip()
    if not token:
        raise SystemExit("X_BEARER_TOKEN missing from .env (app-only Bearer Token, required for /2/webhooks and /2/activity/subscriptions)")
    return token


def get(session: requests.Session, path: str) -> tuple[int, dict]:
    r = session.get(API + path, timeout=30)
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text[:400]}
    return r.status_code, body


def post(session: requests.Session, path: str, payload: dict) -> tuple[int, dict]:
    r = session.post(API + path, json=payload, timeout=30)
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text[:400]}
    return r.status_code, body


def main() -> int:
    ap = argparse.ArgumentParser(description="Inspect/create XAA subscriptions for the mirror listener.")
    ap.add_argument("--apply", action="store_true", help="Actually create subscriptions (default: dry run)")
    ap.add_argument("--list", action="store_true", help="Only list webhooks and existing subscriptions")
    ap.add_argument("--webhook-id", help="Override webhook id (default: the one whose URL matches config)")
    ap.add_argument("--url", required=True,
                    help="The webhook URL registered with X, e.g. https://listen.example.com/webhooks/twitter")
    ap.add_argument("--event-types", default=",".join(DEFAULT_EVENT_TYPES),
                    help="Comma-separated XAA event types (default: post.create)")
    ap.add_argument("--tag", default="x-threads-mirror")
    args = ap.parse_args()

    cfg = load_cfg()
    user_id = str(cfg.get("x_user_id") or "")
    if not user_id:
        raise SystemExit("config.json has no x_user_id")

    token = bearer_token(cfg)
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})

    # 1. Which webhook?
    status, body = get(session, "/2/webhooks")
    if status != 200:
        print(f"GET /2/webhooks -> HTTP {status}: {json.dumps(body)[:300]}")
        return 1
    webhooks = body.get("data") or []
    print(f"webhooks: {len(webhooks)}")
    for wh in webhooks:
        print(f"  id={wh.get('id')} valid={wh.get('valid')} url={wh.get('url')} created={wh.get('created_at')}")

    webhook_id = args.webhook_id
    if not webhook_id:
        match = [w for w in webhooks if (w.get("url") or "").rstrip("/") == args.url.rstrip("/")]
        if not match:
            print(f"\nNo registered webhook matches {args.url}. Register it first with POST /2/webhooks.")
            return 1
        webhook_id = str(match[0]["id"])
        if not match[0].get("valid"):
            print("WARNING: that webhook is marked invalid; run PUT /2/webhooks/{id} to re-validate.")
    print(f"\nusing webhook_id={webhook_id} user_id={user_id}")

    # 2. What already exists?
    status, body = get(session, "/2/activity/subscriptions")
    existing = []
    if status == 200:
        existing = body.get("data") or []
        if isinstance(existing, dict):  # docs disagree on object vs array
            existing = [existing]
    else:
        print(f"GET /2/activity/subscriptions -> HTTP {status}: {json.dumps(body)[:300]}")
        print("(continuing; this endpoint may need a different auth tier)")
    print(f"existing XAA subscriptions: {len(existing)}")
    for sub in existing:
        print(f"  id={sub.get('subscription_id') or sub.get('id')} "
              f"event_type={sub.get('event_type')} filter={json.dumps(sub.get('filter'))} "
              f"webhook_id={sub.get('webhook_id')}")

    wanted = [t.strip() for t in args.event_types.split(",") if t.strip()]

    def already(event_type: str) -> bool:
        for sub in existing:
            if sub.get("event_type") != event_type:
                continue
            filt = sub.get("filter") or {}
            if str(filt.get("user_id") or "") != user_id:
                continue
            if str(sub.get("webhook_id") or "") not in ("", "", None) and str(sub.get("webhook_id")) != webhook_id:
                continue
            return True
        return False

    planned = [t for t in wanted if not already(t)]
    print("\nplanned subscriptions:")
    for et in wanted:
        print(f"  {et}: {'already exists' if et not in planned else 'CREATE'}")
    if not planned or args.list:
        print("\nNothing to create." if not planned else "\n(list mode: no changes)")
        return 0

    if not args.apply:
        print("\nDRY RUN. Re-run with --apply to create the subscriptions above.")
        print("Billing note: each delivered post.create event is billed as a Post ($0.005).")
        return 0

    # 3. Create.
    failures = 0
    for event_type in planned:
        payload = {
            "event_type": event_type,
            "filter": {"user_id": user_id},
            "webhook_id": webhook_id,
            "tag": args.tag,
        }
        status, body = post(session, "/2/activity/subscriptions", payload)
        ok = status in (200, 201)
        print(f"\nPOST /2/activity/subscriptions {event_type} -> HTTP {status}")
        print("  " + json.dumps(body)[:600])
        if not ok:
            failures += 1
    print(f"\n{'CREATED' if not failures else 'PARTIAL'} — {len(planned) - failures}/{len(planned)} subscriptions.")
    print("Verify delivery by watching the listener log for a 'POST accepted ... event types=['data']' line.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
