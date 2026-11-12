#!/usr/bin/env python3
"""replay_backlog.py — re-mirror X posts that were missed while Threads auth was dead.

The mirror is stream-only (X Activity webhook). If the Threads token expired or the
Meta app died, deliveries still arrived but every publish failed, leaving rows with
status='error'. A post that never arrived at all has no state row. This tool walks
the X timeline from the given cutoff; `--reset-errors` first clears every failed
row so those posts are retried too, then anything still unrecorded is mirrored.

It reuses the listener's own modules so the behaviour cannot drift:
  mirror.py           -> run_threads_post, download_media, record_mirror, init_state,
                         reset_error_rows, resolve_dir
  webhook_listener.py -> xaa_event_to_tweet_obj (one v2 -> v1.1 normalizer for both
                         webhook and backfill), tweet_text, extract_tweet_photos,
                         is_original_tweet

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


# Nothing is fetched unless you name a cutoff: this tool reads the X timeline
# (billed per post returned) and re-publishes anything not already in state.db.


def fetch_originals(x_creds, bearer: str, user_id: str, since: str,
                    logger, budget: x_budget.XBudget) -> tuple[list[dict], dict[str, dict]]:
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
    pages = 0
    for _ in range(budget.calls_per_day):
        try:
            budget.note_call()
        except x_budget.XBudgetExceeded as exc:
            print(f"[X-BUDGET] stopping pagination early: {exc}")
            logger.info(f"pagination stopped by spend cap: {exc}")
            break
        params = {
            "max_results": budget.cap_post_count(100),
            # note_tweet / note_post carry the untruncated text of long posts;
            # the listener's normalizer reads whichever X returns.
            "tweet.fields": "created_at,referenced_tweets,attachments,entities,text,note_tweet,note_post",
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
        for item in data.get("includes", {}).get("media", []) or []:
            key = item.get("media_key")
            if key:
                media_lookup[key] = {
                    "media_key": key,
                    "type": item.get("type"),
                    "url": item.get("url"),
                    "preview_image_url": item.get("preview_image_url"),
                }
        token = (data.get("meta") or {}).get("next_token")
        if not token:
            break
        if budget.remaining()["posts_today"] <= 0:
            print("[X-BUDGET] daily post budget spent; stopping pagination")
            logger.info("pagination stopped: daily post budget spent")
            break
        time.sleep(1)
    print(f"[X-BUDGET] fetched {len(tweets)} post(s) in {pages} page(s); {budget.summary()}")
    logger.info(f"fetched {len(tweets)} post(s) in {pages} page(s); {budget.summary()}")
    return tweets, media_lookup


def post_to_xaa_event(post: dict, media_lookup: dict[str, dict],
                      user_id: str, username: str) -> dict:
    """Wrap a fetched v2 Post in the XAA envelope the listener's normalizer reads.

    The listener owns the single v2 -> v1.1-ish conversion (`xaa_event_to_tweet_obj`),
    so backfill and webhook share one code path — including long-post text from
    `note_tweet`/`note_post` and the media-key expansion.
    """
    attachments = post.get("attachments") or {}
    media = [
        media_lookup[str(key)]
        for key in (attachments.get("media_keys") or [])
        if str(key) in media_lookup
    ]
    return {
        "event_type": "post.create",
        "filter": {"user_id": str(user_id)},
        "payload": post,
        "includes": {
            "users": [{"id": str(user_id), "username": username}],
            "media": media,
        },
        "event_uuid": str(post.get("id") or ""),
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
                    help="Delete every state.db row with status='error' so this run retries those posts.")
    args = ap.parse_args()

    # The default is already absolute; a relative --config is repo-relative so the
    # tool behaves the same no matter which directory it is run from.
    cfg_path = Path(args.config).expanduser()
    if not cfg_path.is_absolute():
        cfg_path = (HERE / cfg_path).resolve()
    cfg = json.loads(cfg_path.read_text())
    logger = m.make_logger(HERE / "replay.log", int(cfg["log_max_bytes"]), int(cfg["log_backup_count"]))
    logger.info(f"replay_backlog start dry_run={args.dry_run} since={args.since}")

    budget = x_budget.get_budget(HERE, logger=logger, limits=cfg.get("x_budget"))

    conn = m.init_state(HERE / cfg["state_db_path"])
    env_dir = m.resolve_dir(cfg.get("env_dir"), HERE)
    x_creds = m.load_x_creds(env_dir)
    # The app bearer lives in the same x-post .env. It is the only credential
    # that can read GET /2/users/{id}/tweets (the OAuth1 user token 401s there).
    x_env = m.load_env_file(env_dir / ".env")
    bearer = x_env.get("X_BEARER_TOKEN", "").strip()
    if not bearer:
        raise SystemExit(f"X_BEARER_TOKEN missing from {env_dir / '.env'}")
    cache_dir = m.resolve_dir(cfg["media_cache_dir"], HERE)
    cache_dir.mkdir(parents=True, exist_ok=True)

    if args.reset_errors and not args.dry_run:
        n = m.reset_error_rows(conn)
        print(f"Cleared {n} error row(s) so they can be retried.")
        logger.info(f"cleared {n} error row(s)")
    elif args.reset_errors and args.dry_run:
        n = conn.execute("SELECT count(*) FROM mirrored WHERE status = 'error'").fetchone()[0]
        print(f"(dry run) {n} error row(s) would be cleared for retry.")

    since_dt = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
    user_id = str(cfg["x_user_id"])

    print(f"Fetching X timeline for @{cfg['x_user_handle']} since {args.since} ...")
    raw, media_lookup = fetch_originals(x_creds, bearer, user_id, args.since, logger, budget)
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
        event = post_to_xaa_event(t, media_lookup, user_id, str(cfg["x_user_handle"]))
        tweet_obj = w.xaa_event_to_tweet_obj(event)
        if not tweet_obj:
            continue
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
        # Video-only posts are skipped by policy (photos only). The normalizer puts
        # media under `entities` (and `extended_tweet.entities` for long posts), so
        # check every holder the listener's helpers read.
        media_items: list[dict] = []
        for holder in (tweet_obj.get("extended_entities") or {},
                       tweet_obj.get("entities") or {},
                       (tweet_obj.get("extended_tweet") or {}).get("entities") or {}):
            media_items.extend(holder.get("media") or [])
        has_video = any(
            (it.get("type") in ("video", "animated_gif")) for it in media_items
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
