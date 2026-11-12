#!/usr/bin/env python3
"""Handler-level integration tests — POST real payloads at the built FastAPI app.

These exercise the actual `events()` endpoint: auth, body cap, parsing, filters,
the claim-then-background-publish flow, and observe mode. They are the tests that
catch an indentation/reachability bug in the handler (the 2026-11-09 "body nested
inside the mismatch `if`" regression) and the ones that prove a publish is never
attempted for a duplicate.

No network, no X API, no Threads posts: publishing is monkeypatched (except the
observe case, where `run_threads_post` is), and state lives in an in-memory SQLite
database. Temp `env_dir` and `media_cache_dir`, so nothing is written to the repo.
Run: python3 tests/test_handler_integration.py
"""
import base64
import hashlib
import hmac
import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from fastapi.testclient import TestClient

import mirror as m
import webhook_listener as wl

CONSUMER = "test-consumer-secret"
OAUTH2 = "test-oauth2-client-secret"
UID = "1111111111111111111"          # configured x_user_id in config.example.json
OTHER = "1399175256884064256"        # a different author

failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  <- {extra}"))
    if not cond:
        failures.append(name)


def sign(body: bytes, secret: str = CONSUMER) -> str:
    return "sha256=" + base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


# --- app under test: temp paths + in-memory state -----------------------------
tmp = Path(tempfile.mkdtemp(prefix="xlistener-it-"))
# Prefer the shipped example (CI, fresh clone). On a deployed install there is no
# example next to the tests, so fall back to the real config.json.
_cfg_path = REPO / "config.example.json"
if not _cfg_path.exists():
    _cfg_path = REPO / "config.json"
cfg = wl.load_config(_cfg_path)
cfg["env_dir"] = str(tmp)
cfg["media_cache_dir"] = str(tmp / "media_cache")
cfg["threads_poster_script"] = str(REPO / cfg["threads_poster_script"])
cfg["x_user_id"] = UID
cfg.setdefault("webhook", {})
cfg["webhook"]["observe_only"] = False
cfg["webhook"]["mirror_quote_posts"] = False

logger = logging.getLogger("handler-test")
logger.addHandler(logging.NullHandler())

conn = m.init_state(Path(":memory:"))
app = wl.build_app(cfg, CONSUMER, OAUTH2, conn, logger)
client = TestClient(app)

URL = "/webhooks/twitter"

# Spy on the publish function the background task calls. The handler resolves the
# module-global at request time, so this replacement is what actually runs.
publish_calls = []


def fake_publish(tweet_obj, cfg_, conn_, logger_, cache_dir_):
    tweet_id = str(tweet_obj.get("id_str") or tweet_obj.get("id"))
    publish_calls.append(tweet_id)
    return {"tweet_id": tweet_id, "queued": True}


real_publish = wl.publish_one_tweet
wl.publish_one_tweet = fake_publish


def post_json(payload: dict, signed=True, target=None):
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json"}
    if signed:
        headers["X-Twitter-Webhooks-Signature"] = sign(body)
    return (target or client).post(URL, content=body, headers=headers)


def xaa(event_type, payload, includes=None, user_id=UID):
    return {"data": {
        "event_uuid": "2119821167833797067",
        "created_at": "2026-11-09T15:55:29.000Z",
        "filter": {"user_id": user_id},
        "event_type": event_type,
        "tag": "x-threads-mirror",
        "payload": payload,
        "includes": includes or {},
    }}


def aaa(*tweets, for_user_id=UID):
    return {"for_user_id": for_user_id, "tweet_create_events": list(tweets)}


def xaa_tweet(tweet_id, text, author_id=UID, **extra):
    payload = {"id": tweet_id, "text": text, "author_id": author_id}
    payload.update(extra)
    return payload


# --- signature handling at the endpoint ---
r = post_json({"data": {"event_type": "like.create"}}, signed=False)
check("unsigned POST -> 401", r.status_code == 401, r.status_code)

body = b"{}"
r = client.post(URL, content=body, headers={"X-Twitter-Webhooks-Signature": sign(body)})
check("signed empty payload -> 200", r.status_code == 200, r.status_code)
check("signed empty payload -> nothing processed", r.json() == {"processed": []}, r.text[:120])

# --- OAuth2 header accepted at the endpoint (new scheme) ---
body2 = b"{}"
o2 = sign(body2, OAUTH2)
r = client.post(URL, content=body2, headers={"X-Twitter-Webhooks-Signature-OAuth2": o2})
check("OAuth2-signed POST -> 200", r.status_code == 200, r.status_code)

# --- non-ASCII signature header is a rejection, never a 500 ---
ok, name = wl.verify_signature(
    b"{}", {wl.SIG_HEADER_LEGACY: "sha256=\u00fcnicode"}, OAUTH2, CONSUMER
)
check("non-ASCII signature header rejected (no TypeError)", not ok and name == "", (ok, name))

# --- body cap, checked BEFORE auth ---
too_big = b"x" * (wl.MAX_BODY_BYTES + 1)
r = client.post(URL, content=too_big, headers={"Content-Type": "application/json"})
check("body over MAX_BODY_BYTES -> 413 before auth", r.status_code == 413, r.status_code)

# === an XAA event that matches our user must reach the filter chain ===
r = post_json(xaa("post.create", xaa_tweet("2119821167833797068", "gm", OTHER),
                  {"users": [{"id": OTHER, "username": "someone"}]}))
check("XAA event for our user reaches the pipeline", r.status_code == 200, r.status_code)
check("XAA other-author post is skipped not-author",
      r.json().get("processed", []) and r.json()["processed"][0].get("skipped") == "not-author",
      r.text[:200])

# --- reply from our own account: chain must classify it as reply ---
r = post_json(xaa("post.create", xaa_tweet(
    "2119821167833797069", "@x hi", in_reply_to_tweet_id="123",
    referenced_tweets=[{"type": "replied_to", "id": "123"}]),
    {"users": [{"id": UID, "username": "example_user"}]}))
check("XAA reply from us -> skipped reply",
      r.json().get("processed", []) and r.json()["processed"][0].get("skipped") == "reply",
      r.text[:200])

# --- retweet from our account -> skipped retweet ---
r = post_json(xaa("post.create", xaa_tweet(
    "2119821167833797070", "RT @x: hi",
    referenced_tweets=[{"type": "retweeted", "id": "123"}]),
    {"users": [{"id": UID, "username": "example_user"}]}))
check("XAA repost from us -> skipped retweet",
      r.json().get("processed", []) and r.json()["processed"][0].get("skipped") == "retweet",
      r.text[:200])

# --- quote from our account -> skipped quote (mirror_quote_posts is false) ---
r = post_json(xaa("post.create", xaa_tweet(
    "2119821167833797071", "look", referenced_tweets=[{"type": "quoted", "id": "123"}]),
    {"users": [{"id": UID, "username": "example_user"}]}))
check("XAA quote from us -> skipped quote",
      r.json().get("processed", []) and r.json()["processed"][0].get("skipped") == "quote",
      r.text[:200])

# --- video-only post -> skipped, even with empty text ---
r = post_json(aaa({"id_str": "2119821167833797072", "text": "", "user": {"id_str": UID},
                   "extended_entities": {"media": [{"type": "video"}]}}))
check("video-only post -> skipped video-only",
      r.json().get("processed", []) and r.json()["processed"][0].get("skipped") == "video-only",
      r.text[:200])

# --- AAA format still reaches the chain (author mismatch, so no claim) ---
r = post_json(aaa({"id_str": "2119821167833797073", "text": "gm", "user": {"id_str": OTHER}}))
check("AAA other-author post is skipped not-author",
      r.json().get("processed", []) and r.json()["processed"][0].get("skipped") == "not-author",
      r.text[:200])

# --- for_user_id for a different subscribed user is ignored ---
r = post_json(xaa("post.create", xaa_tweet("2119821167833797074", "gm"),
                  {"users": [{"id": UID, "username": "example_user"}]}, user_id=OTHER))
check("event for a foreign for_user_id is ignored", r.json() == {"processed": []}, r.text[:200])

# --- non-post XAA events are ignored ---
r = post_json(xaa("like.create", {"id": "1"}))
check("XAA like.create ignored", r.json() == {"processed": []}, r.text[:200])

check("nothing was published for a filtered candidate", publish_calls == [], publish_calls)

# === an accepted candidate is claimed and its publish is scheduled ===
VALID_XAA = "2119821167833798001"
r = post_json(xaa("post.create", xaa_tweet(VALID_XAA, "hello from XAA"),
                  {"users": [{"id": UID, "username": "example_user"}]}))
check("valid XAA own-author post -> queued",
      r.json().get("processed", []) == [{"tweet_id": VALID_XAA, "queued": True}], r.text[:200])
row = conn.execute("SELECT status FROM mirrored WHERE tweet_id = ?", (VALID_XAA,)).fetchone()
check("accepted candidate is claimed before publish",
      row is not None and row["status"] == "pending", dict(row) if row else None)
check("publish attempted exactly once for the accepted post",
      publish_calls.count(VALID_XAA) == 1, publish_calls)

# duplicate delivery of the same post -> no second publish
r = post_json(xaa("post.create", xaa_tweet(VALID_XAA, "hello from XAA"),
                  {"users": [{"id": UID, "username": "example_user"}]}))
check("duplicate delivery -> skipped already-mirrored",
      r.json().get("processed", []) and r.json()["processed"][0].get("skipped") == "already-mirrored",
      r.text[:200])
check("duplicate delivery is NOT published again",
      publish_calls.count(VALID_XAA) == 1, publish_calls)

# AAA own-author post also reaches publish (both formats parse end to end)
VALID_AAA = "2119821167833798002"
r = post_json(aaa({"id_str": VALID_AAA, "text": "hello from AAA",
                   "user": {"id_str": UID, "screen_name": "example_user"}}))
check("valid AAA own-author post -> queued",
      r.json().get("processed", []) == [{"tweet_id": VALID_AAA, "queued": True}], r.text[:200])
check("AAA accepted post published exactly once",
      publish_calls.count(VALID_AAA) == 1, publish_calls)

# --- CRC endpoint still signs correctly ---
resp = client.get(URL, params={"crc_token": "abc123"})
expected = "sha256=" + base64.b64encode(hmac.new(OAUTH2.encode(), b"abc123", hashlib.sha256).digest()).decode()
check("CRC signs with OAuth2 client secret", resp.status_code == 200 and resp.json()["response_token"] == expected,
      resp.text[:160])
resp = client.get(URL, params={"crc_token": '{"not":"a token"}'})
check("CRC rejects JSON-looking token", resp.status_code == 400, resp.status_code)

# --- typed-link rule: only X's link anchored at the end is stripped ---
typed = {
    "text": "read https://t.co/typedabc then https://t.co/appended",
    # Media attached via attachments, with no url in entities: _media_short_urls
    # has nothing to remove, so this isolates the end-anchored rule.
    "attachments": {"media_keys": ["3_1"]},
    "entities": {"media": []},
}
check("typed t.co in the middle survives",
      wl.tweet_text(typed) == "read https://t.co/typedabc then", repr(wl.tweet_text(typed)))

typed_no_trailing = {
    "text": "read https://t.co/typedabc now",
    "attachments": {"media_keys": ["3_1"]},
    "entities": {"media": []},
}
check("typed trailing-of-body t.co is not the first-match victim",
      wl.tweet_text(typed_no_trailing) == "read https://t.co/typedabc now",
      repr(wl.tweet_text(typed_no_trailing)))

# --- XAA long posts: note text must not be cut by the truncated text's range ---
note_text = ("A" * 300) + " https://t.co/media"
long_event = {
    "event_type": "post.create",
    "filter": {"user_id": UID},
    "payload": {
        "id": "2119821167833798003",
        "text": note_text[:140],
        "display_text_range": [0, 140],
        "note_post": {"text": note_text},
        "attachments": {"media_keys": []},
    },
    "includes": {},
}
long_tweet = wl.xaa_event_to_tweet_obj(long_event)
check("XAA long post keeps the full note text",
      wl.tweet_text(long_tweet) == note_text, len(wl.tweet_text(long_tweet)))
check("truncated post range is not copied onto extended_tweet",
      "display_text_range" not in (long_tweet.get("extended_tweet") or {}),
      long_tweet.get("extended_tweet", {}).get("display_text_range"))

# --- media allow-list parser agreement ---
check("media allow-list accepts pbs.twimg.com https",
      wl.is_allowed_media_url("https://pbs.twimg.com/media/abc.jpg"))
check("media allow-list rejects backslash", not wl.is_allowed_media_url("https://pbs.twimg.com\\@evil.com/a.jpg"))
check("media allow-list rejects %5c", not wl.is_allowed_media_url("https://pbs.twimg.com%5c@evil.com/a.jpg"))
check("media allow-list rejects userinfo",
      not wl.is_allowed_media_url("https://pbs.twimg.com@evil.com/a.jpg"))
check("media allow-list rejects http", not wl.is_allowed_media_url("http://pbs.twimg.com/a.jpg"))

# === observe mode: dry-run publish, no state.db row, no claim ===
# Restore the real publish path and stub the poster instead, so dry_run is observed.
wl.publish_one_tweet = real_publish
poster_calls = []


def fake_run_threads_post(cfg_, text, image_path, dry_run, logger, image_paths=None):
    poster_calls.append({"text": text, "dry_run": dry_run})
    return {"published": {"id": "should-not-exist"}}


real_run_threads_post = m.run_threads_post
m.run_threads_post = fake_run_threads_post

observe_cfg = json.loads(json.dumps(cfg))
observe_cfg["webhook"]["observe_only"] = True
observe_conn = m.init_state(Path(":memory:"))
observe_client = TestClient(wl.build_app(observe_cfg, CONSUMER, OAUTH2, observe_conn, logger))

OBSERVED = "2119821167833800001"
r = post_json(xaa("post.create", xaa_tweet(OBSERVED, "observe me"),
                  {"users": [{"id": UID, "username": "example_user"}]}), target=observe_client)
check("observe mode -> queued", r.status_code == 200
      and r.json().get("processed", []) == [{"tweet_id": OBSERVED, "queued": True}], r.text[:200])
check("observe mode posts with dry_run=True",
      len(poster_calls) == 1 and poster_calls[0]["dry_run"] is True, poster_calls)
rows = observe_conn.execute("SELECT COUNT(*) FROM mirrored").fetchone()[0]
check("observe mode leaves no state.db row", rows == 0, rows)

# === a failed publish is recorded 'error' and re-claimable by the next delivery ===
attempts = []


def flaky_run_threads_post(cfg_, text, image_path, dry_run, logger, image_paths=None):
    attempts.append(dry_run)
    if len(attempts) == 1:
        raise RuntimeError("threads returned 500")
    return {"published": {"id": "post-ok"}, "post_url": "https://www.threads.com/p/x"}


m.run_threads_post = flaky_run_threads_post
RETRY = "2119821167833800002"
retry_tweet = {"id_str": RETRY, "text": "retry me", "user": {"id_str": UID}}
cache_dir = Path(cfg["media_cache_dir"])
first = wl.mirror_one_tweet(retry_tweet, cfg, conn, logger, cache_dir)
row = conn.execute("SELECT status FROM mirrored WHERE tweet_id = ?", (RETRY,)).fetchone()
check("failed publish is recorded as error, never ok",
      bool(first.get("error")) and row is not None and row["status"] == "error",
      (first, dict(row) if row else None))
second = wl.mirror_one_tweet(retry_tweet, cfg, conn, logger, cache_dir)
row = conn.execute("SELECT status FROM mirrored WHERE tweet_id = ?", (RETRY,)).fetchone()
check("error row is re-claimed and retried on the next delivery",
      second.get("threads_post_id") == "post-ok" and row is not None
      and row["status"] == "ok" and len(attempts) == 2 and attempts[0] is False,
      (second, dict(row) if row else None, attempts))

# restore patches for a clean interpreter state
wl.publish_one_tweet = real_publish
m.run_threads_post = real_run_threads_post
shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("all handler integration tests passed")
