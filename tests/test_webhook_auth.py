#!/usr/bin/env python3
"""Unit tests for webhook auth (CRC + dual signature headers).

No network, no X API, no state.db writes. Uses fake secrets.
Run with the listener venv: venv/bin/python test_webhook_auth.py
"""
import base64
import hashlib
import hmac
import logging
import sys
from pathlib import Path

# Tests live in tests/ in the repo but at the service root on a deployed install.
_HERE = Path(__file__).resolve().parent
REPO = _HERE if (_HERE / "mirror.py").exists() else _HERE.parent
sys.path.insert(0, str(REPO))

import webhook_listener as wl

OAUTH2 = "fake-oauth2-client-secret-aaaa"
CONSUMER = "fake-oauth1-consumer-secret-bbbb"
BODY = b'{"for_user_id":"1111111111111111111","favorite_events":[]}'

failures = []


def check(name, cond):
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}")
        failures.append(name)


def sig(message: bytes, secret: str) -> str:
    d = hmac.new(secret.encode(), message, hashlib.sha256).digest()
    return "sha256=" + base64.b64encode(d).decode()


def verify(headers, oauth2=OAUTH2, consumer=CONSUMER, body=BODY):
    return wl.verify_signature(body, headers, oauth2, consumer)


# --- compute_signature shape ---
check("compute_signature matches manual HMAC",
      wl.compute_signature(BODY, CONSUMER) == sig(BODY, CONSUMER))
check("compute_signature has sha256= prefix",
      wl.compute_signature(BODY, CONSUMER).startswith("sha256="))

# --- OAuth2 header is accepted (the NEW documented path) ---
ok, name = verify({wl.SIG_HEADER_OAUTH2: sig(BODY, OAUTH2)})
check("oauth2 header verifies with oauth2 secret", ok and name == wl.SIG_HEADER_OAUTH2)

# --- legacy header still accepted (backward compat) ---
ok, name = verify({wl.SIG_HEADER_LEGACY: sig(BODY, CONSUMER)})
check("legacy header verifies with consumer secret", ok and name == wl.SIG_HEADER_LEGACY)

# --- both headers present, oauth2 preferred ---
ok, name = verify({
    wl.SIG_HEADER_OAUTH2: sig(BODY, OAUTH2),
    wl.SIG_HEADER_LEGACY: sig(BODY, CONSUMER),
})
check("both headers present -> accepted via oauth2", ok and name == wl.SIG_HEADER_OAUTH2)

# --- cross-keyed headers must FAIL (no scheme confusion) ---
ok, _ = verify({wl.SIG_HEADER_OAUTH2: sig(BODY, CONSUMER)})
check("oauth2 header signed with consumer secret rejected", not ok)
ok, _ = verify({wl.SIG_HEADER_LEGACY: sig(BODY, OAUTH2)})
check("legacy header signed with oauth2 secret rejected", not ok)

# --- missing/absent secrets ---
ok, _ = verify({}, oauth2=OAUTH2, consumer=CONSUMER)
check("no signature headers rejected", not ok)
ok, _ = verify({wl.SIG_HEADER_LEGACY: sig(BODY, CONSUMER)}, oauth2=None)
check("oauth2 unconfigured -> legacy path still works", ok)
ok, _ = verify({wl.SIG_HEADER_OAUTH2: sig(BODY, OAUTH2)}, consumer=None)
check("consumer unconfigured -> oauth2 path still works", ok)

# --- tampered body rejected ---
ok, _ = verify({wl.SIG_HEADER_OAUTH2: sig(BODY, OAUTH2)}, body=BODY + b" ")
check("tampered body rejected", not ok)

# --- CRC signs with oauth2 secret when configured, else consumer secret ---
#
# Mirrors the crc() handler's choice without starting the server.
def crc_token_for(oauth2, consumer, token="abc123"):
    crc_secret = oauth2 or consumer
    return wl.compute_signature(token.encode(), crc_secret)


check("CRC uses oauth2 secret when configured",
      crc_token_for(OAUTH2, CONSUMER) == sig(b"abc123", OAUTH2))
check("CRC falls back to consumer secret",
      crc_token_for(None, CONSUMER) == sig(b"abc123", CONSUMER))

# --- crc_token shape guard still rejects JSON-looking oracle input ---
check("CRC token regex rejects JSON", wl.CRC_TOKEN_RE.fullmatch('{"a":1}') is None)
check("CRC token regex accepts opaque token", wl.CRC_TOKEN_RE.fullmatch("a" * 48) is not None)

print("auth tests done; running XAA tests")

# ============================ XAA (webhook v2) parsing ============================
UID = "1111111111111111111"
POST_ID = "2080761390344937796"


def xaa(event_type, payload, includes=None, user_id=UID):
    return {"data": {
        "event_uuid": "2080761390344937796",
        "created_at": "2026-11-09T12:00:00.000Z",
        "filter": {"user_id": user_id},
        "event_type": event_type,
        "tag": "posts",
        "payload": payload,
        "includes": includes or {},
    }}


USERS = {"users": [{"id": UID, "username": "example_user"}]}

# --- standalone text post ---
c = wl.extract_candidate_tweets(
    xaa("post.create", {"id": POST_ID, "text": "gm", "author_id": UID,
                        "conversation_id": POST_ID, "display_text_range": [0, 2]}, USERS),
    logging.getLogger("t"))
check("XAA text post -> one candidate", len(c) == 1)
check("XAA text post id mapped", c[0].get("id_str") == POST_ID)
check("XAA text post text", c[0].get("text") == "gm")
check("XAA author mapped from includes", c[0]["user"]["id_str"] == UID)
check("XAA filter.user_id carried as for_user_id", c[0].get("for_user_id") == UID)
check("XAA standalone is mirrorable", wl.is_original_tweet(c[0])[0] is True)

# --- photo post: v2 media -> v1.1 shape with downloadable URL ---
c = wl.extract_candidate_tweets(xaa(
    "post.create",
    {"id": POST_ID, "text": "gm https://t.co/abc", "author_id": UID,
     "display_text_range": [0, 2], "attachments": {"media_keys": ["3_1"]}},
    {"users": USERS["users"],
     "media": [{"media_key": "3_1", "type": "photo",
                "url": "https://pbs.twimg.com/media/abc.jpg"}]}), logging.getLogger("t"))
photos = wl.extract_tweet_photos(c[0])
check("XAA photo extracted from includes.media", len(photos) == 1)
check("XAA photo url is https twimg",
      photos and photos[0].get("media_url_https") == "https://pbs.twimg.com/media/abc.jpg")
check("XAA photo url passes media allow-list", bool(photos) and wl.is_allowed_media_url(photos[0]["media_url_https"]))
check("XAA display_text_range strips media t.co link", wl.tweet_text(c[0]) == "gm")

# --- multi-image post: ALL photos must survive extraction (carousel input) ---
c = wl.extract_candidate_tweets(xaa(
    "post.create",
    {"id": POST_ID, "text": "four pics https://t.co/x", "author_id": UID,
     "display_text_range": [0, 9],
     "attachments": {"media_keys": ["3_1", "3_2", "3_3", "3_4"]}},
    {"users": USERS["users"],
     "media": [{"media_key": k, "type": "photo", "url": f"https://pbs.twimg.com/media/{k}.jpg"}
               for k in ("3_1", "3_2", "3_3", "3_4")]}), logging.getLogger("t"))
multi = wl.extract_tweet_photos(c[0])
check("XAA keeps all 4 photos for a carousel", len(multi) == 4)
check("XAA carousel photo order preserved",
      [p["media_url_https"] for p in multi] == [f"https://pbs.twimg.com/media/3_{i}.jpg" for i in (1, 2, 3, 4)])
check("XAA carousel photos all pass the media allow-list",
      all(wl.is_allowed_media_url(p["media_url_https"]) for p in multi))

# --- AAA multi-image payload keeps all 4 too ---
c = wl.extract_candidate_tweets({"for_user_id": UID, "tweet_create_events": [{
    "id_str": POST_ID, "text": "four pics", "user": {"id_str": UID, "screen_name": "example_user"},
    "extended_entities": {"media": [
        {"type": "photo", "media_url_https": f"https://pbs.twimg.com/media/{i}.jpg"} for i in range(1, 5)
    ]}}]}, logging.getLogger("t"))
check("AAA keeps all 4 photos for a carousel", len(wl.extract_tweet_photos(c[0])) == 4)

# --- reply ---
c = wl.extract_candidate_tweets(xaa("post.create", {
    "id": POST_ID, "text": "@x hi", "author_id": UID,
    "in_reply_to_tweet_id": "123", "referenced_tweets": [{"type": "replied_to", "id": "123"}]}, USERS),
    logging.getLogger("t"))
check("XAA reply skipped", wl.is_original_tweet(c[0])[0] is False and "reply" in wl.is_original_tweet(c[0])[1])

# --- repost (retweet) ---
c = wl.extract_candidate_tweets(xaa("post.create", {
    "id": POST_ID, "text": "RT @x: hi", "author_id": UID,
    "referenced_tweets": [{"type": "retweeted", "id": "123"}]}, USERS), logging.getLogger("t"))
check("XAA repost skipped as retweet", wl.is_original_tweet(c[0])[0] is False and "retweet" in wl.is_original_tweet(c[0])[1])

# --- quote ---
c = wl.extract_candidate_tweets(xaa("post.create", {
    "id": POST_ID, "text": "look https://t.co/q", "author_id": UID,
    "referenced_tweets": [{"type": "quoted", "id": "123"}]}, USERS), logging.getLogger("t"))
check("XAA quote flagged as quote", c[0].get("is_quote_status") is True)

# --- long post via note_post ---
c = wl.extract_candidate_tweets(xaa("post.create", {
    "id": POST_ID, "text": "short…", "author_id": UID,
    "note_post": {"text": "the full long post"}}, USERS), logging.getLogger("t"))
check("XAA note_post full text wins", wl.tweet_text(c[0]) == "the full long post")

# --- other XAA event types are ignored ---
c = wl.extract_candidate_tweets(xaa("like.create", {"id": "1"}), logging.getLogger("t"))
check("XAA like.create ignored", c == [])

# --- AAA payload still accepted unchanged ---
AAA = {"for_user_id": UID, "tweet_create_events": [{
    "id_str": POST_ID, "text": "gm", "user": {"id_str": UID, "screen_name": "example_user"}}]}
c = wl.extract_candidate_tweets(AAA, logging.getLogger("t"))
check("AAA payload still parsed", len(c) == 1 and c[0]["id_str"] == POST_ID)
check("AAA form not misdetected as XAA", wl.is_xaa_payload(AAA) is False)
check("XAA form detected", wl.is_xaa_payload(xaa("post.create", {})) is True)

# ============================ CRC key selection ============================
check("crc auto prefers oauth2", wl.select_crc_secret(OAUTH2, CONSUMER)[0] == OAUTH2)
check("crc auto falls back to legacy", wl.select_crc_secret(None, CONSUMER)[0] == CONSUMER)
check("crc can be pinned to legacy while oauth2 exists", wl.select_crc_secret(OAUTH2, CONSUMER, "legacy")[0] == CONSUMER)
check("crc can be pinned to oauth2", wl.select_crc_secret(OAUTH2, CONSUMER, "oauth2")[0] == OAUTH2)
check("crc bogus override behaves as auto", wl.select_crc_secret(OAUTH2, CONSUMER, "bogus")[0] == OAUTH2)
check("crc no secrets returns None", wl.select_crc_secret(None, None)[0] is None)


# ==================== no back-links to the X post in mirrored text ====================
# Regression: an AAA payload arrived WITHOUT display_text_range, so the t.co URL X
# appends for attached media was published to Threads as a link back to the post.

def _tweet(text, rng=None, media=True, refs=None):
    d = {"id_str": "1", "text": text, "user": {"id_str": UID}}
    if rng is not None:
        d["display_text_range"] = rng
    if refs:
        d["referenced_tweets"] = refs
    if media:
        m = [{"type": "photo", "url": "https://t.co/MEDIA",
              "media_url_https": "https://pbs.twimg.com/media/x.jpg"}]
        d["entities"] = {"media": m}
        d["extended_entities"] = {"media": m}
    return d

_LEAK = "When you get her on the custom self-hosted harness  https://t.co/JstzdB5DWR"
_WORDS = "When you get her on the custom self-hosted harness"
_AUTHOR_LINK = "check https://example.com/thing"

check("media t.co stripped when display_text_range is missing",
      wl.tweet_text(_tweet(_LEAK)) == _WORDS)
check("media t.co stripped when display_text_range is present",
      wl.tweet_text(_tweet(_LEAK, rng=[0, len(_WORDS)])) == _WORDS)
check("quote post's appended status link stripped",
      wl.tweet_text(_tweet("my take https://t.co/QUOTE", media=False,
                           refs=[{"type": "quoted", "id": "9"}])) == "my take")
check("author's own link in a text-only post is preserved",
      wl.tweet_text(_tweet("new post https://example.com/thing", media=False))
      == "new post https://example.com/thing")
check("author's own link inside display_text_range is preserved",
      wl.tweet_text(_tweet(_AUTHOR_LINK, rng=[0, len(_AUTHOR_LINK)])) == _AUTHOR_LINK)
check("media post with no link is untouched",
      wl.tweet_text(_tweet("just the words")) == "just the words")

# XAA path: the envelope carries the t.co in text and may omit display_text_range
_xaa_leak = wl.xaa_event_to_tweet_obj(xaa(
    "post.create", {"id": POST_ID, "text": _LEAK, "author_id": UID,
                    "attachments": {"media_keys": ["3_1"]}},
    {"users": USERS["users"],
     "media": [{"media_key": "3_1", "type": "photo", "url": "https://pbs.twimg.com/media/x.jpg"}]})["data"])
check("XAA media t.co stripped when display_text_range is missing",
      wl.tweet_text(_xaa_leak) == _WORDS)


print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("all auth + XAA tests passed")
