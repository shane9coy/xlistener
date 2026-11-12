#!/usr/bin/env python3
"""Shared plumbing for XListener.

Everything both entry points need: config and .env loading, the SQLite
dedupe/claim ledger, media download from X's image CDN, and the subprocess wrapper
around `threads_poster.py`.

There is no polling loop. The live path is the webhook listener
(`webhook_listener.py`); `tools/replay_backlog.py` is the explicit backfill tool.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"

# How long a claimed-but-unfinished post blocks a retry before the claim is
# considered abandoned (crashed worker, killed process) and may be taken over.
CLAIM_STALE_SECONDS = 900

# Child processes (the Threads poster and its curl uploads) get this minimal
# environment instead of ours, so credentials exported in the parent shell are
# never inherited by a subprocess.
SAFE_CHILD_ENV_KEYS = (
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
)


# ---------- config ----------

def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path) as f:
        return json.load(f)


def resolve_dir(value: str | None, base: Path = HERE) -> Path:
    """Resolve a config path against the repo, never against the process CWD."""
    if not value:
        return base
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else (base / candidate).resolve()


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


def load_x_creds(env_dir: Path) -> tuple[str, str, str, str]:
    """X OAuth 1.0a user-context credentials from <env_dir>/.env."""
    env = load_env_file(env_dir / ".env")
    required = ["X_CONSUMER_KEY", "X_CONSUMER_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]
    missing = [k for k in required if not env.get(k)]
    if missing:
        raise SystemExit(f"Missing X creds in {env_dir / '.env'}: {missing}")
    return env["X_CONSUMER_KEY"], env["X_CONSUMER_SECRET"], env["X_ACCESS_TOKEN"], env["X_ACCESS_TOKEN_SECRET"]


def load_threads_creds(env_dir: Path) -> tuple[str, str]:
    env = load_env_file(env_dir / ".env")
    token = env.get("THREADS_ACCESS_TOKEN", "").strip()
    uid = env.get("THREADS_USER_ID", "").strip()
    if not token or not uid:
        raise SystemExit(f"Missing Threads creds in {env_dir / '.env'}")
    return token, uid


# ---------- logging ----------

def make_logger(log_path: Path, max_bytes: int, backup_count: int) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(log_path.stem)
    logger.setLevel(logging.INFO)
    if logger.handlers:  # re-entrant calls must not duplicate handlers
        return logger
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = RotatingFileHandler(log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


# ---------- state ----------
#
# Row lifecycle:
#   pending -> claimed by a delivery, publishing right now
#   ok      -> published (never retried)
#   error   -> an attempt failed; the next delivery of the same post may retry it
#
# The claim is taken BEFORE any slow work, so duplicate deliveries and X's retries
# cannot double-post.

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mirrored (
    tweet_id          TEXT PRIMARY KEY,
    mirrored_at       TEXT NOT NULL,
    threads_post_id   TEXT,
    threads_post_url  TEXT,
    source_url        TEXT,
    error             TEXT,
    status            TEXT NOT NULL DEFAULT 'ok'
);
"""

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_mirrored_at ON mirrored(mirrored_at);
CREATE INDEX IF NOT EXISTS idx_mirrored_status ON mirrored(status);
"""

# The connection is shared with handlers that FastAPI runs on worker threads.
# Serialize statements through one lock; WAL keeps readers cheap.
_DB_LOCK = threading.RLock()


def init_state(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(TABLE_SQL)
    # Migrate pre-status databases in place; existing failures become retryable.
    # Indexes are created AFTER the migration so an old table without `status` works.
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(mirrored)")}
    if "status" not in cols:
        conn.execute("ALTER TABLE mirrored ADD COLUMN status TEXT NOT NULL DEFAULT 'ok'")
        conn.execute("UPDATE mirrored SET status='error' WHERE error IS NOT NULL AND error != ''")
    conn.executescript(INDEX_SQL)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    return conn


def _row(conn: sqlite3.Connection, tweet_id: str):
    return conn.execute(
        "SELECT status, mirrored_at, threads_post_id FROM mirrored WHERE tweet_id = ?",
        (tweet_id,),
    ).fetchone()


def is_already_mirrored(conn: sqlite3.Connection, tweet_id: str) -> bool:
    """True when this post must not be published again.

    Error rows are deliberately NOT 'already mirrored': a transient failure should be
    retried by the next delivery instead of being dropped forever.
    """
    with _DB_LOCK:
        row = _row(conn, tweet_id)
    return row is not None and row["status"] in ("ok", "pending")


def claim_mirror(conn: sqlite3.Connection, tweet_id: str, source_url: str) -> bool:
    """Atomically claim a post for publishing. True if this caller owns it.

    A stale `pending` row (crashed worker) and any `error` row can be re-claimed;
    `ok` rows never can.
    """
    now = now_iso()
    with _DB_LOCK:
        row = _row(conn, tweet_id)
        if row is None:
            conn.execute(
                "INSERT INTO mirrored (tweet_id, mirrored_at, source_url, status) VALUES (?, ?, ?, 'pending')",
                (tweet_id, now, source_url),
            )
            conn.commit()
            return True
        if row["status"] == "ok":
            return False
        if row["status"] == "pending":
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["mirrored_at"])).total_seconds()
            except (TypeError, ValueError):
                age = CLAIM_STALE_SECONDS + 1
            if age < CLAIM_STALE_SECONDS:
                return False  # another worker is actively publishing this post
        conn.execute(
            "UPDATE mirrored SET status='pending', mirrored_at=?, error=NULL WHERE tweet_id = ?",
            (now, tweet_id),
        )
        conn.commit()
        return True


def record_mirror(
    conn: sqlite3.Connection,
    tweet_id: str,
    threads_post_id: str | None,
    threads_post_url: str | None,
    source_url: str,
    error: str | None,
) -> None:
    with _DB_LOCK:
        conn.execute(
            """INSERT INTO mirrored
                   (tweet_id, mirrored_at, threads_post_id, threads_post_url, source_url, error, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(tweet_id) DO UPDATE SET
                   mirrored_at=excluded.mirrored_at,
                   threads_post_id=excluded.threads_post_id,
                   threads_post_url=excluded.threads_post_url,
                   source_url=excluded.source_url,
                   error=excluded.error,
                   status=excluded.status""",
            (tweet_id, now_iso(), threads_post_id, threads_post_url, source_url, error,
             "error" if error else "ok"),
        )
        conn.commit()


def release_claim(conn: sqlite3.Connection, tweet_id: str) -> None:
    """Drop a claim without recording an outcome (abandoned run)."""
    with _DB_LOCK:
        conn.execute("DELETE FROM mirrored WHERE tweet_id = ? AND status = 'pending'", (tweet_id,))
        conn.commit()


def reset_error_rows(conn: sqlite3.Connection) -> int:
    """Delete failed rows so a backfill can retry them. Returns rows removed."""
    with _DB_LOCK:
        n = conn.execute("DELETE FROM mirrored WHERE status = 'error'").rowcount
        conn.commit()
        return n


# ---------- time ----------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- media ----------

def download_media(url: str, dest: Path, logger: logging.Logger, timeout: float = 30.0) -> Path:
    """Download a media URL to dest. Redirects are NOT followed (X's CDN is direct)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    max_bytes = 20 * 1024 * 1024  # Threads rejects images far smaller than this anyway
    with requests.get(url, stream=True, timeout=timeout, allow_redirects=False) as r:
        r.raise_for_status()
        if r.status_code != 200:
            raise RuntimeError(f"media fetch returned {r.status_code}, not following redirects")
        written = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    written += len(chunk)
                    if written > max_bytes:
                        f.close()
                        dest.unlink(missing_ok=True)
                        raise RuntimeError("media larger than 20 MB, refused")
                    f.write(chunk)
    logger.info(f"downloaded media → {dest}")
    return dest


def prune_media_cache(cache_dir: Path, max_age_hours: float, logger: logging.Logger | None = None) -> int:
    """Delete cached media older than max_age_hours. Returns the number removed.

    The webhook path downloads every image it mirrors; without this the directory
    grows forever. Called after each successful mirror.
    """
    if max_age_hours <= 0 or not cache_dir.exists():
        return 0
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    for item in cache_dir.iterdir():
        try:
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink()
                removed += 1
        except OSError:
            continue
    if removed and logger:
        logger.info(f"pruned {removed} cached media file(s) older than {max_age_hours:g}h")
    return removed


# ---------- subprocess wrapper ----------

def safe_child_env() -> dict[str, str]:
    """A minimal environment for child processes: no credentials, no stray exports."""
    return {k: os.environ[k] for k in SAFE_CHILD_ENV_KEYS if k in os.environ}


def run_threads_post(
    cfg: dict,
    text: str,
    image_path: Path | None,
    dry_run: bool,
    logger: logging.Logger,
    image_paths: list[Path] | None = None,
) -> dict:
    """Call the Threads poster. Returns parsed JSON from stdout.

    1 photo -> single image post, 2+ -> carousel. `--dry-run` builds the payloads and
    publishes nothing (and uploads nothing). The child environment is scrubbed; the
    poster reads its own .env file.
    """
    repo_dir = resolve_dir(cfg.get("env_dir"), HERE)
    script = repo_dir / cfg.get("threads_poster_script", "threads_poster.py")
    if not script.exists():
        raise RuntimeError(f"Threads post script missing: {script}")

    py = cfg.get("python_bin") or sys.executable
    cmd = [py, str(script)]
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

    timeout = float(cfg.get("publish_timeout_seconds") or 150)
    logger.info(f"exec: {' '.join(shlex_quote(c) for c in cmd)}")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(repo_dir),
            env=safe_child_env(),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"threads-post timed out after {timeout:g}s") from None
    if proc.returncode != 0:
        raise RuntimeError(f"threads-post failed (rc={proc.returncode}): {proc.stderr.strip()}")
    # The result is the JSON object at the end of stdout. Upload progress printed
    # before it must not turn a published post into an "error" here.
    out = proc.stdout
    start = next((mo.start() for mo in re.finditer(r"(?m)^\{", out)), None)
    try:
        return json.loads(out if start is None else out[start:])
    except json.JSONDecodeError as e:
        raise RuntimeError(f"threads-post returned non-JSON: {proc.stdout[:500]} ({e})") from None


def shlex_quote(s: str) -> str:
    return shlex.quote(s)
