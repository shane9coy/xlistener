#!/usr/bin/env python3
"""replay_backlog.py — re-mirror X posts that were missed while Threads auth was dead.

The mirror is stream-only (X Account Activity webhook). If the Threads token
expired or the Meta app died, the webhook still fires and the listener still
records a row in state.db for every attempt — but every publish fails. Those
posts are never retried automatically, because the dedup table treats a failed
row as "already handled".

This tool walks the X timeline from the given cutoff, applies the exact same
filter rules the listener uses (originals only: no replies, no retweets, no
quote posts, no video-only), and re-mirrors everything that is not in state.db.

It reuses the daemon's own modules so the behaviour cannot drift:
  mirror.py         -> run_threads_post, download_media, record_mirror, init_state
  webhook_listener.py -> tweet_text, extract_tweet_photos, is_original_tweet

USAGE
  python3 tools/replay_backlog.py --dry-run      # report what would be posted
  python3 tools/replay_backlog.py                # actually post
  python3 tools/replay_backlog.py --since 2026-09-28 --limit 5
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import mirror as m  # noqa: E402
import webhook_listener as w  # noqa: E402
import x_budget  # noqa: E402  (hard spend caps on X reads)

import tweepy  # noqa: E402

# Nothing is fetched unless you name a cutoff: this tool reads the X timeline
# (billed per post returned) and re-publishes anything not already in state.db.


def fetch_originals(x_creds, bearer: str, user_id: str, since: str) -> list[dict]:
    """Fetch the user's timeline, paginated, keeping only original posts.

    Auth: the OAuth2 app bearer, NOT the OAuth1 user token. Verified
    2026-11-08: the OAuth1 user token can read get_me() but returns
    401 on GET /2/users/{id}/tweets, while the app bearer reads the
    timeline fine and returns the pbs.twimg.com media URLs directly in
    the media expansion (so no /2/media/upload call is needed).

    Returns (tweets, media_lookup) where media_lookup maps media_key ->
    {media_key, type, url, preview_image_url}.
    """
    import requests

    headers = {"Authorization": f"Bearer {bearer}"}
    tweets: list[dict] = []
    media_lookup: dict[str, dict] = {}
    token = None
    # SPEND GUARD: X bills per post returned. Without this, 10 pages x 100 posts
    # = 1000 posts = $5.00 in one command. The budget clamps the page size
    # (default 10/call) and refuses calls past the daily call/post caps.
    budget = x_budget.get_budget(HERE)
    pages = 0
    for _ in range(budget.calls_per_day):
        try:
            budget.note_call()
        except x_budget.XBudgetExceeded as exc:
            print(f"[X-BUDGET] stopping pagination early: {exc}")
            break
        params = {
            "max_results": budget.cap_post_count(100),
            "tweet.fields": "created_at,referenced_tweets,attachments,entities,text",
            "expansions": "attachments.media_keys",
            "media.fields": "url,type,preview_image_url",
            "exclude": "replies,retweets",
        }
        if token:
            params["pagination_token"] = token
        resp = requests.get(
            f"https://api.x.com/2/users/{user_id}/tweets",
            headers=headers, params=params, timeout=30,
        )
        if resp.status_code != 200:
            raise SystemExit(f"X timeline fetch failed: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        batch = data.get("data") or []
        budget.note_posts(len(batch))
        pages += 1
        tweets.extend(batch)
        for m in data.get("includes", {}).get("media", []) or []:
            key = m.get("media_key")
            if key:
                media_lookup[key] = {
                    "media_key": key,
                    "type": m.get("type"),
                    "url": m.get("url"),
                    "preview_image_url": m.get("preview_image_url"),
                }
        token = (data.get("meta") or {}).get("next_token")
        if not token:
            break
        if budget.remaining()["posts_today"] <= 0:
            print("[X-BUDGET] daily post budget spent; stopping pagination")
            break
        time.sleep(1)
    print(f"[X-BUDGET] fetched {len(tweets)} post(s) in {pages} page(s); {budget.summary()}")
    return tweets, media_lookup


def build_v2_tweet(t: dict, media_lookup: dict) -> dict:
    """Normalise an X API v2 tweet into the v1.1-ish shape the listener expects.

    The listener's helpers (tweet_text / extract_tweet_photos / is_original_tweet)
    were written against the AAA webhook payload, which is v1.1-shaped. We
    convert so the same code paths — and therefore the same filtering — apply.
    """
    att = t.get("attachments") or {}
    media = []
    for key in (att.get("media_keys") or []):
        m_item = media_lookup.get(key)
        if not m_item:
            continue
        media.append({
            "type": m_item.get("type"),
            "media_url_https": m_item.get("url") or m_item.get("preview_image_url"),
            "media_key": key,
        })
    entities = {"media": media}
    referenced = t.get("referenced_tweets") or []
    return {
        "id_str": str(t.get("id")),
        "id": str(t.get("id")),
        "text": t.get("text") or "",
        "full_text": t.get("text") or "",
        "created_at": t.get("created_at"),
        "entities": entities,
        "extended_entities": entities,
        "referenced_tweets": referenced,
        "is_quote_status": any((r or {}).get("type") == "quoted" for r in referenced),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Re-mirror X posts missed during a Threads outage.")
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--since", required=True,
                    help="ISO timestamp cutoff, e.g. 2026-11-01T00:00:00Z. Required.")
    ap.add_argument("--limit", type=int, default=200, help="Max posts to mirror in this run.")
    ap.add_argument("--dry-run", action="store_true", help="Report only; do not post.")
    ap.add_argument("--include-quotes", action="store_true", help="Also mirror quote posts (off by default).")
    ap.add_argument("--reset-errors", action="store_true",
                    help="Delete state.db rows whose error was a Threads publish failure, so they retry.")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    logger = m.make_logger(HERE / "replay.log", int(cfg["log_max_bytes"]), int(cfg["log_backup_count"]))
    logger.info(f"replay_backlog start dry_run={args.dry_run} since={args.since}")

    conn = m.init_state(HERE / cfg["state_db_path"])
    env_dir = Path(cfg.get("env_dir") or HERE)
    x_creds = m.load_x_creds(env_dir)
    # The app bearer lives in the same x-post .env. It is the only credential
    # that can read GET /2/users/{id}/tweets (the OAuth1 user token 401s there).
    x_env = m.load_env_file(env_dir / ".env")
    bearer = x_env.get("X_BEARER_TOKEN", "").strip()
    if not bearer:
        raise SystemExit("X_BEARER_TOKEN missing from .env")
    threads_post_dir = env_dir
    cache_dir = (HERE / cfg["media_cache_dir"]).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)

    if args.reset_errors and not args.dry_run:
        n = conn.execute(
            "DELETE FROM mirrored WHERE error LIKE 'post error: threads-post failed%'"
        ).rowcount
        conn.commit()
        print(f"Cleared {n} failed-publish rows so they can be retried.")
        logger.info(f"cleared {n} failed-publish rows")
    elif args.reset_errors and args.dry_run:
        n = conn.execute(
            "SELECT count(*) FROM mirrored WHERE error LIKE 'post error: threads-post failed%'"
        ).fetchone()[0]
        print(f"(dry run) {n} failed-publish row(s) would be cleared for retry.")

    since_dt = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
    user_id = str(cfg["x_user_id"])

    print(f"Fetching X timeline for @{cfg['x_user_handle']} since {args.since} ...")
    raw, media_lookup = fetch_originals(x_creds, bearer, user_id, args.since)
    print(f"Fetched {len(raw)} tweets (replies+retweets excluded at the API); "
          f"{len(media_lookup)} media keys resolved.")

    already = {r[0] for r in conn.execute("SELECT tweet_id FROM mirrored")}

    candidates = []
    for t in raw:
        created = t.get("created_at")
        try:
            created_dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        except Exception:
            continue
        if created_dt < since_dt:
            continue
        tid = str(t.get("id"))
        if tid in already:
            continue
        tweet_obj = build_v2_tweet(t, media_lookup)
        is_quote = bool(tweet_obj.get("is_quote_status"))
        if is_quote and not args.include_quotes:
            continue
        ok, reason = w.is_original_tweet(tweet_obj)
        if not ok:
            continue
        photos = w.extract_tweet_photos(tweet_obj)
        text = w.tweet_text(tweet_obj)
        if not photos and not text:
            continue
        # Video-only posts are skipped by policy (photos only). Mirror the same
        # rule the webhook listener applies: video present AND no photos -> skip.
        has_video = any(
            (it.get("type") in ("video", "animated_gif"))
            for it in (tweet_obj.get("extended_entities", {}) or {}).get("media", [])
        )
        if has_video and not photos:
            continue
        candidates.append((tid, created, tweet_obj, photos, text))

    # Oldest first, so the Threads timeline reads in order.
    candidates.sort(key=lambda c: str(c[1]))
    candidates = candidates[: args.limit]

    print(f"\n{len(candidates)} post(s) to mirror:")
    for tid, created, _, photos, text in candidates:
        print(f"  {tid}  {str(created)[:16]}  {'photo' if photos else 'text':5}  {text[:64]!r}")

    if not candidates:
        print("\nNothing to do.")
        return

    if args.dry_run:
        print("\nDRY RUN — nothing posted. Re-run without --dry-run to publish.")
        return

    published = failed = 0
    for tid, created, tweet_obj, photos, text in candidates:
        source_url = f"https://x.com/{cfg['x_user_handle']}/status/{tid}"
        local_photos = []
        try:
            for idx, photo in enumerate(photos):
                url = photo.get("media_url_https") or photo.get("media_url")
                if not url:
                    continue
                if not w.is_allowed_media_url(url):
                    raise ValueError(f"media url not on X's image CDN: {url}")
                ext = ".jpg"
                if ".png" in url.lower():
                    ext = ".png"
                elif ".webp" in url.lower():
                    ext = ".webp"
                dest = cache_dir / f"{tid}-{idx}{ext}"
                m.download_media(url, dest, logger)
                local_photos.append(dest)
        except Exception as e:
            print(f"  [ERR download] {tid}: {e}")
            m.record_mirror(conn, tid, None, None, source_url, f"download error: {e}")
            failed += 1
            continue

        try:
            result = m.run_threads_post(
                cfg, text, local_photos[0] if len(local_photos) == 1 else None, False, logger,
                image_paths=local_photos,
            )
            post_id = (result.get("published") or {}).get("id") or result.get("container_id")
            post_url = result.get("post_url")
            m.record_mirror(conn, tid, post_id, post_url, source_url, None)
            print(f"  [OK] {tid} -> {post_url or post_id}")
            published += 1
        except Exception as e:
            print(f"  [ERR post] {tid}: {e}")
            m.record_mirror(conn, tid, None, None, source_url, f"post error: {e}")
            failed += 1
        time.sleep(3)  # be polite to the Threads API

    print(f"\nDone. published={published} failed={failed}")
    logger.info(f"replay done published={published} failed={failed}")


if __name__ == "__main__":
    main()
