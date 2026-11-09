#!/usr/bin/env python3
"""Handler-level integration tests — POST real payloads at the built FastAPI app.

This is the test class that was missing: it exercises the actual `events()` endpoint,
so an indentation/reachability bug in the handler (the 2026-11-09 "body nested inside
the mismatch `if`" regression) fails a test instead of silently dropping every event.

No network, no X API, no Threads posts, no state.db writes: every case below is a path
that is filtered out BEFORE mirror_one_tweet() is called.
Run: venv/bin/python test_handler_integration.py
"""
import base64
import hashlib
import hmac
import logging
import sys
from pathlib import Path

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from fastapi.testclient import TestClient

import mirror as m
import webhook_listener as wl

CONSUMER = "test-consumer-secret"
OAUTH2 = "test-oauth2-client-secret"
UID = "1111111111111111111"          # configured x_user_id in config.json
OTHER = "1399175256884064256"        # a different author

failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  <- {extra}"))
    if not cond:
        failures.append(name)


def sign(body: bytes) -> str:
    return "sha256=" + base64.b64encode(hmac.new(CONSUMER.encode(), body, hashlib.sha256).digest()).decode()


cfg = wl.load_config(REPO / "config.example.json")
_tmp = Path(__import__("tempfile").mkdtemp(prefix="xlistener-cfg-"))
cfg["env_dir"] = str(_tmp)
cfg["media_cache_dir"] = str(_tmp / "media_cache")
cfg["threads_poster_script"] = str(REPO / cfg["threads_poster_script"])
logger = logging.getLogger("handler-test")
logger.addHandler(logging.NullHandler())
app = wl.build_app(cfg, CONSUMER, OAUTH2, m.init_state(Path(":memory:")), logger)
client = TestClient(app)

URL = "/webhooks/twitter"


def post_json(payload: dict, signed=True) -> "TestClient.response":
    import json
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json"}
    if signed:
        headers["X-Twitter-Webhooks-Signature"] = sign(body)
    return client.post(URL, content=body, headers=headers)


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


# --- signature handling at the endpoint ---
r = post_json({"data": {"event_type": "like.create"}}, signed=False)
check("unsigned POST -> 401", r.status_code == 401, r.status_code)

body = b'{}'
r = client.post(URL, content=body, headers={"X-Twitter-Webhooks-Signature": sign(body)})
check("signed empty payload -> 200", r.status_code == 200, r.status_code)
check("signed empty payload -> nothing processed", r.json() == {"processed": []}, r.text[:120])

# --- OAuth2 header accepted at the endpoint (new scheme) ---
body2 = b'{}'
o2 = "sha256=" + base64.b64encode(hmac.new(OAUTH2.encode(), body2, hashlib.sha256).digest()).decode()
r = client.post(URL, content=body2, headers={"X-Twitter-Webhooks-Signature-OAuth2": o2})
check("OAuth2-signed POST -> 200", r.status_code == 200, r.status_code)

# === THE REGRESSION: an XAA event that matches our user must reach the filter chain ===
# author != our user -> the chain runs and skips "not-author". If the handler body is
# nested inside the for_user_id guard, this returns processed=[] and the test fails.
r = post_json(xaa("post.create", {"id": "2119821167833797068", "text": "gm",
                                  "author_id": OTHER},
                  {"users": [{"id": OTHER, "username": "someone"}]}))
check("XAA event for our user reaches the pipeline", r.status_code == 200, r.status_code)
check("XAA other-author post is skipped not-author",
      r.json().get("processed", []) and r.json()["processed"][0].get("skipped") == "not-author",
      r.text[:200])

# --- reply from our own account: chain must classify it as reply ---
r = post_json(xaa("post.create", {"id": "2119821167833797069", "text": "@x hi",
                                  "author_id": UID, "in_reply_to_tweet_id": "123",
                                  "referenced_tweets": [{"type": "replied_to", "id": "123"}]},
                  {"users": [{"id": UID, "username": "example_user"}]}))
check("XAA reply from us -> skipped reply",
      r.json().get("processed", []) and r.json()["processed"][0].get("skipped") == "reply",
      r.text[:200])

# --- retweet from our account -> skipped retweet ---
r = post_json(xaa("post.create", {"id": "2119821167833797070", "text": "RT @x: hi",
                                  "author_id": UID,
                                  "referenced_tweets": [{"type": "retweeted", "id": "123"}]},
                  {"users": [{"id": UID, "username": "example_user"}]}))
check("XAA repost from us -> skipped retweet",
      r.json().get("processed", []) and r.json()["processed"][0].get("skipped") == "retweet",
      r.text[:200])

# --- quote from our account -> skipped quote (mirror_quote_posts is false) ---
r = post_json(xaa("post.create", {"id": "2119821167833797071", "text": "look",
                                  "author_id": UID,
                                  "referenced_tweets": [{"type": "quoted", "id": "123"}]},
                  {"users": [{"id": UID, "username": "example_user"}]}))
check("XAA quote from us -> skipped quote",
      r.json().get("processed", []) and r.json()["processed"][0].get("skipped") == "quote",
      r.text[:200])

# --- AAA format still reaches the chain (author mismatch, so no db write) ---
r = post_json({"for_user_id": UID, "tweet_create_events": [
    {"id_str": "2119821167833797072", "text": "gm", "user": {"id_str": OTHER}}]})
check("AAA other-author post is skipped not-author",
      r.json().get("processed", []) and r.json()["processed"][0].get("skipped") == "not-author",
      r.text[:200])

# --- for_user_id for a different subscribed user is ignored ---
r = post_json(xaa("post.create", {"id": "2119821167833797073", "text": "gm", "author_id": UID},
                  {"users": [{"id": UID, "username": "example_user"}]}, user_id=OTHER))
check("event for a foreign for_user_id is ignored", r.json() == {"processed": []}, r.text[:200])

# --- non-post XAA events are ignored ---
r = post_json(xaa("like.create", {"id": "1"}))
check("XAA like.create ignored", r.json() == {"processed": []}, r.text[:200])

# --- CRC endpoint still signs correctly ---
resp = client.get(URL, params={"crc_token": "abc123"})
expected = "sha256=" + base64.b64encode(hmac.new(OAUTH2.encode(), b"abc123", hashlib.sha256).digest()).decode()
check("CRC signs with OAuth2 client secret", resp.status_code == 200 and resp.json()["response_token"] == expected,
      resp.text[:160])
resp = client.get(URL, params={"crc_token": '{"not":"a token"}'})
check("CRC rejects JSON-looking token", resp.status_code == 400, resp.status_code)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("all handler integration tests passed")
