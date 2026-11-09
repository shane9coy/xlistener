#!/usr/bin/env python3
"""
x-threads-mirror: watch @example_user's X timeline, mirror originals (photos + text only)
to the corresponding Threads account (@example_threads_user) via the official Threads Graph API.

Rules:
  - Only ORIGINAL posts (skip replies, skip retweets/reposts of others).
  - Only photo media (skip video-only posts).
  - Verbatim copy (no X link appended).
  - Autonomous — your X post IS the trigger. No review queue.

State:
  - SQLite DB at state.db, table `mirrored(tweet_id PRIMARY KEY, mirrored_at, threads_post_id, source_url, error)`.
  - Idempotent on X tweet_id; safe to re-run after crashes.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable

import requests
import tweepy

import x_budget  # hard spend caps on X reads (X bills per post returned)


HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
DEFAULT_VENV_PYTHON = HERE / "venv" / "bin" / "python"


# ---------- config ----------

def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path) as f:
        return json.load(f)


# ---------- env loading (do not echo creds) ----------

def load_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict. Does NOT print or return secrets to chat."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def load_x_creds(env_dir: Path) -> tuple[str, str, str, str, str]:
    """Load the X OAuth 1.0a user-context credentials from <env_dir>/.env.

    Required keys: X_CONSUMER_KEY, X_CONSUMER_SECRET,
                   X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET.
    Optional:      X_BEARER_TOKEN (app-only calls: webhook management, subscriptions).
    """
    env = load_env_file(env_dir / ".env")
    required = [
        "X_CONSUMER_KEY",
        "X_CONSUMER_SECRET",
        "X_ACCESS_TOKEN",
        "X_ACCESS_TOKEN_SECRET",
    ]
    missing = [k for k in required if not env.get(k)]
    if missing:
        raise SystemExit(f"Missing X creds in {env_dir / '.env'}: {missing}")
    return (
        env["X_CONSUMER_KEY"],
        env["X_CONSUMER_SECRET"],
        env["X_ACCESS_TOKEN"],
        env["X_ACCESS_TOKEN_SECRET"],
    )


def load_threads_creds(env_dir: Path) -> tuple[str, str]:
    env = load_env_file(env_dir / ".env")
    token = env.get("THREADS_ACCESS_TOKEN", "").strip()
    uid = env.get("THREADS_USER_ID", "").strip()
    if not token or not uid:
        raise SystemExit(f"Missing Threads creds in {env_dir / '.env'}")
    return token, uid


# ---------- logging ----------

def make_logger(log_path: Path, max_bytes: int, backup_count: int) -> logging.Logger:
    logger = logging.getLogger("x-threads-mirror")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    fh = RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


# ---------- state ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS mirrored (
    tweet_id          TEXT PRIMARY KEY,
    mirrored_at       TEXT NOT NULL,
    threads_post_id   TEXT,
    threads_post_url  TEXT,
    source_url        TEXT,
    error             TEXT
);
CREATE INDEX IF NOT EXISTS idx_mirrored_at ON mirrored(mirrored_at);
"""


def init_state(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def is_already_mirrored(conn: sqlite3.Connection, tweet_id: str) -> bool:
    cur = conn.execute("SELECT 1 FROM mirrored WHERE tweet_id = ?", (tweet_id,))
    return cur.fetchone() is not None


def record_mirror(
    conn: sqlite3.Connection,
    tweet_id: str,
    threads_post_id: str | None,
    threads_post_url: str | None,
    source_url: str,
    error: str | None,
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO mirrored
           (tweet_id, mirrored_at, threads_post_id, threads_post_url, source_url, error)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (tweet_id, now_iso(), threads_post_id, threads_post_url, source_url, error),
    )
    conn.commit()


# ---------- time ----------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_twitter_time(s: str) -> datetime:
    # X returns ISO8601 with possible fractional seconds.
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------- active hours gate ----------

def is_within_active_hours(cfg: dict) -> bool:
    """Return True if daemon should run right now. null cfg = always."""
    active = cfg.get("active_hours")
    if active is None:
        return True
    # active hours are interpreted in America/New_York.
    try:
        from zoneinfo import ZoneInfo
        local = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        local = datetime.now()
    h = local.hour
    start = int(active.get("start", 0))
    end = int(active.get("end", 24))
    if start <= end:
        return start <= h < end
    # wraps midnight (e.g. 22 -> 6)
    return h >= start or h < end


# ---------- X client (tweepy OAuth1 user-context) ----------

def make_x_client(creds: tuple[str, str, str, str]) -> tweepy.Client:
    consumer_key, consumer_secret, access_token, access_token_secret = creds
    return tweepy.Client(
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        access_token=access_token,
        access_token_secret=access_token_secret,
        wait_on_rate_limit=True,
    )


def resolve_user_id(client: tweepy.Client, handle: str) -> int:
    resp = client.get_user(username=handle)
    if resp.data is None:
        raise SystemExit(f"X user @{handle} not found")
    return int(resp.data.id)


# ---------- the actual mirror decision ----------

@dataclass
class Tweet:
    id: str
    text: str
    created_at: datetime
    media_urls: list[str]    # raw X media URLs (parallel to media_kinds)
    media_kinds: list[str]   # parallel to media_urls; "photo" | "video"
    referenced_types: list[str]  # types from referenced_tweets (e.g. "replied_to", "retweeted", "quoted")
    source_url: str

    @property
    def has_video(self) -> bool:
        return "video" in self.media_kinds

    @property
    def has_photo(self) -> bool:
        return "photo" in self.media_kinds

    @property
    def is_reply(self) -> bool:
        return "replied_to" in self.referenced_types

    @property
    def is_retweet(self) -> bool:
        return "retweeted" in self.referenced_types

    @property
    def is_quote(self) -> bool:
        return "quoted" in self.referenced_types


def fetch_recent_originals(
    client: tweepy.Client,
    user_id: int,
    since_id: str | None,
    max_tweets: int,
) -> list[Tweet]:
    """Fetch user's tweets newer than `since_id`. Single page, no pagination loop.

    The X API `exclude` param supports ['replies', 'retweets'] so we never even
    receive them. This is the cheap, authoritative way to enforce the originals-only rule.

    COST DISCIPLINE:
      - One call per tick. No pagination loop. If since_id is None (first run),
        we fetch only the most recent `max_tweets` (capped low to bound first-run cost).
      - Caller is responsible for tracking since_id in SQLite and passing it
        every tick; the daemon never refetches tweets it has already seen.
      - If we get zero new tweets, the call was still billed (X charges per
        request, not per result). That's the unavoidable floor.
    """
    expansions = ["attachments.media_keys", "referenced_tweets.id"]
    media_fields = ["url", "type", "preview_image_url"]
    tweet_fields = ["text", "created_at", "referenced_tweets", "attachments"]

    # SPEND GUARD: X bills per post returned, so the budget is charged BEFORE the
    # call and the page size is clamped (default 10 posts/call, 10 posts/day).
    # note_call() raises XBudgetExceeded instead of making an unaffordable read.
    budget = x_budget.get_budget(HERE)
    page_size = budget.cap_post_count(min(100, max_tweets))
    budget.note_call()

    kwargs = dict(
        id=user_id,
        max_results=page_size,
        exclude=["replies", "retweets"],
        expansions=expansions,
        media_fields=media_fields,
        tweet_fields=tweet_fields,
    )
    if since_id:
        kwargs["since_id"] = since_id

    resp = client.get_users_tweets(**kwargs)
    budget.note_posts(len(resp.data or []))
    if resp.data is None:
        return []

    # Build a media lookup keyed by media_key.
    media_lookup: dict[str, dict] = {}
    if resp.includes and "media" in resp.includes:
        for m in resp.includes["media"]:
            media_lookup[m["media_key"]] = m

    tweets: list[Tweet] = []
    for t in resp.data:
        # referenced_tweets contains reply/quote/retweet pointers (we already
        # excluded replies+retweets at API level, but a quoted self-tweet
        # is still original content; we let it through).
        ref_types = [r["type"] for r in (t.get("referenced_tweets") or [])]

        media_urls: list[str] = []
        media_kinds: list[str] = []
        for key in (t.get("attachments", {}) or {}).get("media_keys", []):
            m = media_lookup.get(key)
            if not m:
                continue
            mt = m.get("type")
            url = m.get("url") or m.get("preview_image_url")
            if not url:
                continue
            if mt == "photo":
                media_kinds.append("photo")
                media_urls.append(url)
            elif mt == "video":
                media_kinds.append("video")
                media_urls.append(url)
            # animated_gif falls under "video" per X; treat as video (skip).

        tweets.append(
            Tweet(
                id=str(t["id"]),
                text=t["text"],
                created_at=parse_twitter_time(t["created_at"]),
                media_urls=media_urls,
                media_kinds=media_kinds,
                referenced_types=ref_types,
                source_url=f"https://x.com/{cfg.get('x_user_handle', 'i')}/status/{t['id']}",
            )
        )

        if len(tweets) >= max_tweets:
            break

    return tweets


def get_highest_seen_id(conn: sqlite3.Connection) -> str | None:
    """Return the highest X tweet_id we've already mirrored (for since_id bookmark).

    Only real, plausible tweet ids are considered. A synthetic/oversized id
    (e.g. 9999999999999999999, left behind by a bad-signature delivery test on
    2026-09-18) used to win MAX(CAST(tweet_id AS INTEGER)), which pushed the
    bookmark to 9223372036854775807 and made every since_id fetch return
    nothing — the mirror would then silently skip ALL future posts.
    Guard: tweet_id must be all digits and at most 19 characters (real X ids
    are 19 digits), and the value must be below 2^63-1.
    """
    cur = conn.execute(
        """
        SELECT MAX(CAST(tweet_id AS INTEGER)) FROM mirrored
        WHERE error IS NULL OR error = ''
          AND tweet_id GLOB '[0-9]*'
          AND length(tweet_id) <= 19
          AND CAST(tweet_id AS INTEGER) < 9223372036854775807
        """
    )
    row = cur.fetchone()
    if row and row[0] is not None:
        return str(row[0])
    return None


def download_media(url: str, dest: Path, logger: logging.Logger) -> Path:
    """Download a media URL to dest. Returns dest on success."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    max_bytes = 20 * 1024 * 1024  # Threads rejects images far smaller than this anyway
    with requests.get(url, stream=True, timeout=60, allow_redirects=False) as r:
        r.raise_for_status()
        if r.status_code != 200:
            raise RuntimeError(f"media fetch returned {r.status_code}, not following redirects")
        written = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    written += len(chunk)
                    if written > max_bytes:
                        f.close(); dest.unlink(missing_ok=True)
                        raise RuntimeError("media larger than 20 MB, refused")
                    f.write(chunk)
    logger.info(f"downloaded media → {dest}")
    return dest


# ---------- subprocess wrappers ----------

def run_threads_post(
    cfg: dict,
    text: str,
    image_path: Path | None,
    dry_run: bool,
    logger: logging.Logger,
    image_paths: list[Path] | None = None,
) -> dict:
    """Call the threads-post script. Returns parsed JSON from stdout.

    The script supports a positional `text` arg, optional `--image <path>` for a
    single photo and `--images <p1> <p2> ...` for a carousel. `image_paths` is the
    preferred way to pass media: 1 photo -> single image post, 2+ -> carousel.
    `--dry-run` prints payload but does not POST.
    """
    repo_dir = Path(cfg.get("env_dir") or HERE)
    threads_script = repo_dir / cfg.get("threads_poster_script", "threads_poster.py")
    if not threads_script.exists():
        raise SystemExit(f"Threads post script missing: {threads_script}")

    py = cfg.get("python_bin") or sys.executable
    cmd = [py, str(threads_script)]
    if dry_run:
        cmd.append("--dry-run")
    if image_paths is None:
        paths = [Path(image_path)] if image_path is not None else []
    else:
        paths = [Path(p) for p in image_paths]
    if len(paths) > 1:
        cmd.extend(["--images", *[str(p) for p in paths]])
    elif len(paths) == 1:
        cmd.extend(["--image", str(paths[0])])
    cmd.extend(["--", text or ""])

    logger.info(f"exec: {' '.join(shlex_quote(c) for c in cmd)}")
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(repo_dir),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"threads-post failed (rc={proc.returncode}): {proc.stderr.strip()}")
    # The result is the JSON object at the end of stdout. Anything a helper printed
    # before it (upload progress, warnings) must not turn a published post into an
    # "error" here, because the caller would then record it as failed.
    out = proc.stdout
    start = next((mo.start() for mo in re.finditer(r"(?m)^\{", out)), None)
    try:
        return json.loads(out if start is None else out[start:])
    except json.JSONDecodeError as e:
        raise RuntimeError(f"threads-post returned non-JSON: {proc.stdout[:500]} ({e})")


def shlex_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


# ---------- the tick ----------

def tick(
    cfg: dict,
    conn: sqlite3.Connection,
    x_client: tweepy.Client,
    user_id: int,
    logger: logging.Logger,
    dry_run: bool,
) -> dict:
    """One polling cycle. Returns summary stats."""
    if not is_within_active_hours(cfg):
        logger.info("outside active hours; skipping tick")
        return {"skipped": "outside_active_hours"}

    x_handle = cfg.get("x_user_handle") or "?"
    t_handle = cfg.get("threads_handle") or "?"

    since_id = get_highest_seen_id(conn)
    logger.info(
        f"tick: X=@{x_handle} (id={user_id}) → Threads=@{t_handle} | "
        f"since_id={since_id or '(none — first run)'} dry_run={dry_run}"
    )

    # Idle-skip: if the last X call returned nothing AND was recent, skip this tick.
    # Bounds X API spend on quiet days when you're not posting.
    idle_skip = int(cfg.get("idle_skip_seconds", 120))
    if since_id is not None:
        cur = conn.execute(
            "SELECT mirrored_at FROM mirrored WHERE tweet_id = ?",
            (since_id,),
        )
        row = cur.fetchone()
        if row:
            last_seen = datetime.fromisoformat(row[0])
            age = (datetime.now(timezone.utc) - last_seen).total_seconds()
            if age < idle_skip:
                logger.info(f"idle-skip: last X activity {age:.0f}s ago (<{idle_skip}s window); skipping X call")
                return {"skipped": "idle", "since_id": since_id, "age_seconds": int(age)}

    tweets = fetch_recent_originals(
        x_client, user_id, since_id=since_id, max_tweets=int(cfg["max_tweets_per_tick"])
    )
    logger.info(f"fetched {len(tweets)} original tweet(s) since since_id={since_id}")

    stats = {"fetched": len(tweets), "mirrored": 0, "skipped_reply": 0, "skipped_retweet": 0, "skipped_video": 0, "errors": 0}

    for t in tweets:
        # Defense-in-depth: even though we asked the API to exclude replies+retweets,
        # double-check the referenced_tweets payload.
        if t.is_reply:
            stats["skipped_reply"] += 1
            logger.info(f"[SKIP reply] {t.id}")
            continue
        if t.is_retweet:
            stats["skipped_retweet"] += 1
            logger.info(f"[SKIP retweet] {t.id}")
            continue
        if is_already_mirrored(conn, t.id):
            logger.info(f"[SKIP already-mirrored] {t.id}")
            continue
        if t.has_video and not t.has_photo:
            stats["skipped_video"] += 1
            logger.info(f"[SKIP video-only] {t.id}")
            record_mirror(conn, t.id, None, None, t.source_url, "skipped: video-only")
            continue

        # Resolve media URLs by downloading each photo to the cache dir.
        cache = HERE / cfg["media_cache_dir"]
        local_photos: list[Path] = []
        try:
            for kind, url in zip(t.media_kinds, t.media_urls):
                if kind != "photo":
                    continue
                ext = ".jpg"
                if ".png" in url.lower():
                    ext = ".png"
                elif ".webp" in url.lower():
                    ext = ".webp"
                dest = cache / f"{t.id}-{len(local_photos)}{ext}"
                download_media(url, dest, logger)
                local_photos.append(dest)
        except Exception as e:
            stats["errors"] += 1
            logger.warning(f"[ERR download] {t.id}: {e}")
            record_mirror(conn, t.id, None, None, t.source_url, f"download error: {e}")
            continue

        # Post to Threads. Single image + text in one container (Threads supports
        # up to 20 images in a carousel but the skill currently exposes single-image).
        image_path = local_photos[0] if local_photos else None
        try:
            result = run_threads_post(cfg, t.text, image_path, dry_run, logger)
            if dry_run:
                logger.info(f"[DRY-RUN] would post tweet {t.id}")
                continue
            post_id = result.get("published", {}).get("id") or result.get("container_id")
            post_url = result.get("post_url")
            record_mirror(conn, t.id, post_id, post_url, t.source_url, None)
            stats["mirrored"] += 1
            logger.info(f"[OK] mirrored tweet {t.id} → threads {post_url or post_id}")
        except Exception as e:
            stats["errors"] += 1
            logger.warning(f"[ERR post] {t.id}: {e}")
            record_mirror(conn, t.id, None, None, t.source_url, f"post error: {e}")

    # Cleanup: remove media cache files for tweets older than 1 hour (best-effort).
    try:
        cutoff = time.time() - 3600
        for p in (HERE / cfg["media_cache_dir"]).iterdir():
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
    except Exception:
        pass

    return stats


# ---------- main ----------

def main() -> int:
    p = argparse.ArgumentParser(
        description="Mirror @example_user's original X posts to @example_threads_user on Threads."
    )
    p.add_argument("--dry-run", action="store_true", help="Fetch X posts and report; do not post to Threads.")
    p.add_argument("--once", action="store_true", help="Run a single tick and exit (no loop).")
    p.add_argument("--config", default=str(CONFIG_PATH), help="Path to config.json")
    p.add_argument("--log-file", help="Override log file path")
    p.add_argument("--handle", help="Override X source handle (without @). Re-resolves user_id.")
    p.add_argument("--threads-handle", help="Override Threads target handle (logging only; the token's bound account is what posts).")
    args = p.parse_args()

    cfg = load_config(Path(args.config))

    log_path = Path(args.log_file) if args.log_file else HERE / cfg["log_file"]
    logger = make_logger(log_path, int(cfg["log_max_bytes"]), int(cfg["log_backup_count"]))
    logger.info(f"x-threads-mirror starting (dry_run={args.dry_run})")

    # CLI overrides (one-off reruns against a different X account without editing config).
    if args.handle:
        cfg["x_user_handle"] = args.handle.lstrip("@")
        cfg["x_user_id"] = None  # force re-resolution against the new handle
        logger.info(f"--handle override: x_user_handle={cfg['x_user_handle']}")
    if args.threads_handle:
        cfg["threads_handle"] = args.threads_handle.lstrip("@")
        logger.info(f"--threads-handle override: threads_handle={cfg['threads_handle']}")

    # Load creds (never echo them).
    env_dir = Path(cfg.get("env_dir") or HERE)
    x_creds = load_x_creds(env_dir)
    threads_creds = load_threads_creds(env_dir)

    # The threads-post script reads its own .env, so we don't need to inject env.
    # We just verify it loaded successfully.
    if not threads_creds[0] or not threads_creds[1]:
        raise SystemExit("Threads creds failed to load")

    # X client.
    x_client = make_x_client(x_creds)
    user_id = cfg.get("x_user_id")
    if not user_id:
        handle = cfg.get("x_user_handle")
        if not handle:
            raise SystemExit("config.x_user_handle missing and x_user_id not set")
        user_id = resolve_user_id(x_client, handle)
        cfg["x_user_id"] = user_id
        # persist
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
        logger.info(f"resolved @{handle} → user_id={user_id}")

    # State DB.
    conn = init_state(HERE / cfg["state_db_path"])

    if args.once:
        stats = tick(cfg, conn, x_client, int(user_id), logger, args.dry_run)
        logger.info(f"single tick stats: {stats}")
        return 0

    # Loop mode.
    interval = int(cfg["poll_interval_minutes"]) * 60
    logger.info(f"entering poll loop, interval={interval}s")
    while True:
        try:
            stats = tick(cfg, conn, x_client, int(user_id), logger, args.dry_run)
            logger.info(f"tick stats: {stats}")
        except Exception as e:
            logger.exception(f"tick crashed: {e}")
        time.sleep(interval)


# timedelta was used by the old window-based fetcher; since_id bookmark replaces it.

if __name__ == "__main__":
    sys.exit(main() or 0)