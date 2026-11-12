#!/usr/bin/env python3
"""State-ledger, media allow-list, and text-extraction regressions.

Covers the review findings that had no test:
  - the claim ledger (no double publish, failures retryable, stale claims takeable)
  - the pre-status -> status schema migration
  - the CDN allow-list rejecting parser-trick URLs
  - typed links surviving when display_text_range is absent
  - XAA long posts not being truncated by the payload's truncated-text range
  - a non-ASCII signature header being rejected instead of raising

Offline only: no network, no credentials, no X/Meta calls.
Run: venv/bin/python tests/test_mirror_state.py
"""
import base64
import hashlib
import hmac
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Tests live in tests/ in the repo but at the service root on a deployed install.
_HERE = Path(__file__).resolve().parent
REPO = _HERE if (_HERE / "mirror.py").exists() else _HERE.parent
sys.path.insert(0, str(REPO))

import mirror as m  # noqa: E402
import webhook_listener as wl  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  <- {extra}"))
    if not cond:
        failures.append(name)


TMP = Path(tempfile.mkdtemp(prefix="xlistener-state-"))
DB = TMP / "state.db"
SRC = "https://x.com/example_user/status/1"

conn = m.init_state(DB)

# ---------------- claim ledger ----------------
check("first claim wins", m.claim_mirror(conn, "111", SRC) is True)
check("second claim of the same post is refused", m.claim_mirror(conn, "111", SRC) is False)
check("in-flight post counts as already mirrored", m.is_already_mirrored(conn, "111") is True)

m.record_mirror(conn, "111", "post_1", "https://www.threads.net/@x/post/1", SRC, None)
check("ok row refuses a new claim", m.claim_mirror(conn, "111", SRC) is False)
check("ok row is already-mirrored", m.is_already_mirrored(conn, "111") is True)

# failure -> retryable
check("claim for a fresh post", m.claim_mirror(conn, "222", SRC) is True)
m.record_mirror(conn, "222", None, None, SRC, "post error: boom")
check("error row is NOT already-mirrored (retryable)", m.is_already_mirrored(conn, "222") is False)
check("error row can be re-claimed", m.claim_mirror(conn, "222", SRC) is True)

# stale pending takeover
check("claim for stale test post", m.claim_mirror(conn, "333", SRC) is True)
old = (datetime.now(timezone.utc) - timedelta(seconds=m.CLAIM_STALE_SECONDS + 60)).isoformat(timespec="seconds")
conn.execute("UPDATE mirrored SET mirrored_at = ? WHERE tweet_id = '333'", (old,))
conn.commit()
check("stale pending claim can be taken over", m.claim_mirror(conn, "333", SRC) is True)

# release_claim drops an abandoned claim only
check("claim before release", m.claim_mirror(conn, "444", SRC) is True)
m.release_claim(conn, "444")
check("released claim is claimable again", m.claim_mirror(conn, "444", SRC) is True)
m.record_mirror(conn, "444", "p", "u", SRC, None)
check("release_claim never touches ok rows", m.claim_mirror(conn, "444", SRC) is False)

# reset_error_rows
before = conn.execute("SELECT count(*) FROM mirrored WHERE status='error'").fetchone()[0]
m.record_mirror(conn, "555", None, None, SRC, "post error: nope")
removed = m.reset_error_rows(conn)
check("reset_error_rows reports what it deleted", removed >= 1, removed)
check("reset_error_rows removes failed rows", conn.execute("SELECT count(*) FROM mirrored WHERE status='error'").fetchone()[0] == 0)
check("reset_error_rows keeps published rows", conn.execute("SELECT count(*) FROM mirrored WHERE tweet_id='111' AND status='ok'").fetchone()[0] == 1)

# ---------------- schema migration ----------------
OLD = TMP / "old.db"
raw = sqlite3.connect(OLD)
raw.executescript("""
CREATE TABLE mirrored (
    tweet_id TEXT PRIMARY KEY, mirrored_at TEXT NOT NULL, threads_post_id TEXT,
    threads_post_url TEXT, source_url TEXT, error TEXT);
INSERT INTO mirrored (tweet_id, mirrored_at, source_url, error) VALUES ('9','2026-01-01T00:00:00+00:00','u',NULL);
INSERT INTO mirrored (tweet_id, mirrored_at, source_url, error) VALUES ('8','2026-01-01T00:00:00+00:00','u','post error: old');
""")
raw.commit(); raw.close()
migrated = m.init_state(OLD)
check("migration adds the status column", "status" in {r["name"] for r in migrated.execute("PRAGMA table_info(mirrored)")})
check("migration keeps successes as ok", migrated.execute("SELECT status FROM mirrored WHERE tweet_id='9'").fetchone()["status"] == "ok")
check("migration marks old failures retryable", migrated.execute("SELECT status FROM mirrored WHERE tweet_id='8'").fetchone()["status"] == "error")

# ---------------- media allow-list ----------------
good = ("https://pbs.twimg.com/media/abc.jpg", True)
cases = [
    good,
    ("http://pbs.twimg.com/media/abc.jpg", False),
    ("https://evil.com/x.jpg", False),
    ("https://pbs.twimg.com.evil.com/x.jpg", False),
    ("https://evil.com\\@pbs.twimg.com/x.jpg", False),
    ("https://user@evil.com/pbs.twimg.com/x.jpg", False),
    ("https://pbs.twimg.com%5c@evil.com/x.jpg", False),
]
for url, want in cases:
    check(f"allow-list {want and 'accepts' or 'rejects'} {url[:48]}", wl.is_allowed_media_url(url) is want,
          wl.is_allowed_media_url(url))

# ---------------- text extraction ----------------
def tweet(text, rng=None, media=True, refs=None):
    d = {"id_str": "1", "text": text, "user": {"id_str": "1"}}
    if rng is not None:
        d["display_text_range"] = rng
    if refs:
        d["referenced_tweets"] = refs
    if media:
        med = [{"type": "photo", "url": "https://t.co/MEDIA",
                "media_url_https": "https://pbs.twimg.com/media/x.jpg"}]
        d["entities"] = {"media": med}
        d["extended_entities"] = {"media": med}
    return d


check("trailing media t.co stripped when no range",
      wl.tweet_text(tweet("words  https://t.co/MEDIA")) == "words")
check("typed mid-text link is preserved when no range",
      wl.tweet_text(tweet("read https://t.co/TYPED then https://t.co/MEDIA")) == "read https://t.co/TYPED then",
      wl.tweet_text(tweet("read https://t.co/TYPED then https://t.co/MEDIA")))
check("author link inside a range is preserved",
      wl.tweet_text(tweet("see https://example.com/x", rng=[0, 25])) == "see https://example.com/x")

LONG = "L" * 600
ev = wl.xaa_event_to_tweet_obj({
    "event_type": "post.create", "filter": {"user_id": "1"},
    "payload": {"id": "77", "text": "short", "author_id": "1",
                "display_text_range": [0, 280], "note_post": {"text": LONG}},
    "includes": {},
})
check("XAA long post is not truncated by the payload range",
      len(wl.tweet_text(ev)) == len(LONG), len(wl.tweet_text(ev)))

# ---------------- non-ASCII signature header ----------------
try:
    ok, _ = wl.verify_signature(b"{}", {"x-twitter-webhooks-signature": "sha256=\xff"}, None, "secret")
    check("non-ASCII signature header is rejected, not raised", ok is False)
except TypeError as exc:  # pragma: no cover
    check("non-ASCII signature header is rejected, not raised", False, f"TypeError: {exc}")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("all state/allow-list/text tests passed")
