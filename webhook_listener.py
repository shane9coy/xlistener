#!/usr/bin/env python3
"""
x-threads-mirror webhook listener (stream-only mode).

Receives X Account Activity API webhook events, filters to @example_user's
original posts (no replies, no retweets, no video-only), and mirrors them to
@example_threads_user's Threads account via the existing threads-post skill.

This replaces the polling daemon. There is no polling — only this listener.

Endpoints:
  GET  /webhooks/twitter              CRC handshake (X calls this on registration,
                                       and roughly hourly to verify the URL is live)
  POST /webhooks/twitter              Event delivery (HMAC-signed with consumer secret)
  GET  /health                        Operator health check
  GET  /oauth/callback/{nonce}        Optional PUBLIC OAuth redirect target for
                                       Threads. The default flow (tools/threads_oauth.py)
                                       uses a localhost callback, so this route is only
                                       needed when the redirect must be a public host.
                                       Requires a one-time nonce in the path.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import re
import sqlite3
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import requests
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# Reuse the polling daemon's plumbing — config load, env load, state, threads-post subprocess.
import mirror as m


HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"


# ---------- config + logger ----------

def load_config(path: Path = CONFIG_PATH) -> dict:
    return m.load_config(path)


def make_logger(log_path: Path, max_bytes: int, backup_count: int) -> logging.Logger:
    return m.make_logger(log_path, max_bytes, backup_count)


# X's OAuth 2.0 client secret lives beside the OAuth 1.0a creds in x-post/.env.
# Accepted key names, first match wins. Absent is allowed (OAuth1-only apps), and
# the listener then falls back to the legacy header/consumer secret exactly as before.
OAUTH2_SECRET_KEYS = ("X_OAUTH2_CLIENT_SECRET",)


def load_oauth2_client_secret(env_dir: Path) -> str | None:
    """Return the X OAuth 2.0 client secret, or None if the app has none configured."""
    env = m.load_env_file(env_dir / ".env")
    for key in OAUTH2_SECRET_KEYS:
        val = (env.get(key) or "").strip()
        if val:
            return val
    return None


# ---------- HMAC verification ----------

# X sends one of two signature headers on every webhook POST (see
# https://docs.x.com/x-api/webhooks/introduction):
#
#   X-Twitter-Webhooks-Signature-OAuth2  HMAC keyed with the OAuth 2.0 CLIENT SECRET  (recommended)
#   X-Twitter-Webhooks-Signature         HMAC keyed with the OAuth 1.0 consumer secret (legacy)
#
# Apps with both secrets configured may receive both headers during migration; apps
# with only an OAuth 2.0 client secret receive ONLY the OAuth2 header. A listener
# that accepts just the legacy header therefore rejects every delivery from a
# modern app. The CRC response_token is keyed the same way.
SIG_HEADER_OAUTH2 = "x-twitter-webhooks-signature-oauth2"
SIG_HEADER_LEGACY = "x-twitter-webhooks-signature"


def compute_signature(message: bytes, secret: str) -> str:
    """sha256= + base64(HMAC-SHA256(secret, message))."""
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    return "sha256=" + base64.b64encode(digest).decode("ascii")


def verify_signature(
    raw_body: bytes,
    headers: Any,
    oauth2_secret: str | None,
    consumer_secret: str | None,
) -> tuple[bool, str]:
    """Accept a POST if EITHER signature header verifies against ITS OWN secret.

    Returns (ok, header_name_that_matched). Each header is checked only against the
    secret X documents for it, so a wrong/absent secret for one scheme cannot weaken
    the other. Comparison is timing-safe.

    Header lookup is normalized to lowercase so this works with Starlette's
    case-insensitive Headers and with a plain dict.
    """
    if not hasattr(headers, "items"):
        return False, ""
    lowered = {str(k).lower(): v for k, v in headers.items()}
    for header_name, secret in (
        (SIG_HEADER_OAUTH2, oauth2_secret),
        (SIG_HEADER_LEGACY, consumer_secret),
    ):
        if not secret:
            continue
        header_value = lowered.get(header_name)
        if not header_value:
            continue
        expected = compute_signature(raw_body, secret)
        if hmac.compare_digest(expected, header_value):
            return True, header_name
    return False, ""


CRC_KEY_MODES = ("auto", "oauth2", "legacy")


def select_crc_secret(
    oauth2_secret: str | None,
    consumer_secret: str | None,
    override: str = "auto",
) -> tuple[str | None, str]:
    """Pick which secret keys the CRC response_token, and name it for the log.

    X keys the CRC token with the OAuth 2.0 client secret when the app has one, and
    with the OAuth 1.0 consumer secret only for OAuth1-only apps. `override` lets the
    operator pin the scheme explicitly ('oauth2' or 'legacy') when the two disagree.
    """
    mode = (override or "auto").strip().lower()
    if mode not in CRC_KEY_MODES:
        mode = "auto"
    if mode == "legacy":
        return consumer_secret, "legacy-oauth1-consumer-secret (pinned)"
    if mode == "oauth2":
        return oauth2_secret, "oauth2-client-secret (pinned)"
    if oauth2_secret:
        return oauth2_secret, "oauth2-client-secret"
    return consumer_secret, "legacy-oauth1-consumer-secret"


# ---------- CRC token + media URL allow-lists ----------

# The CRC reply and the event signature are the SAME construction:
# base64(HMAC-SHA256(consumer_secret, <bytes>)). If GET /webhooks/twitter signs
# whatever it is handed, it is a signing oracle: anyone can ask it to sign a
# JSON body of their choosing and then POST that body with a valid signature,
# which posts to Threads as us. X's real crc_token is a short opaque token, so
# only that shape is signed. No JSON object can match it (it must start with
# "{"), which is what closes the oracle.
CRC_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-+/=.:~]{1,256}$")

# Media is only ever fetched from X's own image CDN, over https. Anything else
# in a payload is refused rather than requested from this machine.
MEDIA_HOST_SUFFIXES = (".twimg.com",)


def is_allowed_media_url(url: str) -> bool:
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host.endswith(sfx) for sfx in MEDIA_HOST_SUFFIXES)


# ---------- X event filtering ----------

def is_original_tweet(tweet_obj: dict) -> tuple[bool, str]:
    """Return (should_mirror, skip_reason_if_not).

    A tweet is an "original" (mirrorable) if:
      - NOT a reply (no in_reply_to_status_id_str / in_reply_to_user_id_str)
      - NOT a retweet of someone else (no retweeted_status)
      - Has at least one photo OR is text-only (we accept text-only)

    Note: quote tweets of others ARE original content; we mirror those too.
    """
    # Both payload shapes: v1.1 objects (in_reply_to_*_str, retweeted_status) and
    # v2 objects (in_reply_to_user_id, referenced_tweets[].type).
    ref_types = {(r or {}).get("type") for r in (tweet_obj.get("referenced_tweets") or [])}
    if (tweet_obj.get("in_reply_to_status_id_str") or tweet_obj.get("in_reply_to_user_id_str")
            or tweet_obj.get("in_reply_to_status_id") or tweet_obj.get("in_reply_to_user_id")
            or "replied_to" in ref_types):
        return False, "reply"
    if "retweeted" in ref_types:
        return False, "retweet"
    if tweet_obj.get("retweeted_status") or str(tweet_obj.get("text") or "").startswith("RT @"):
        return False, "retweet"
    return True, ""


def tweet_text(tweet_obj: dict) -> str:
    """The post as its author wrote it.

    X's v1.1-shaped payload truncates `text` at 140 characters and keeps the whole
    post under `extended_tweet.full_text`. `display_text_range` marks where the
    human-written part ends; what follows is the t.co link X appends for attached
    media, which means nothing on Threads.
    """
    ext = tweet_obj.get("extended_tweet") or {}
    src = ext if ext.get("full_text") else tweet_obj
    text = src.get("full_text") or tweet_obj.get("text") or ""
    rng = src.get("display_text_range")
    if isinstance(rng, list) and len(rng) == 2 and all(isinstance(n, int) for n in rng) and 0 <= rng[0] < rng[1] <= len(text):
        text = text[rng[0]:rng[1]]
    return text.strip()


def extract_tweet_photos(tweet_obj: dict) -> list[dict]:
    """Pull photo entities from a tweet object. Returns [] for video-only or text-only."""
    out: list[dict] = []
    # Long posts carry their media under extended_tweet; look there first.
    ext = tweet_obj.get("extended_tweet") or {}
    for holder in (ext.get("extended_entities") or {}, ext.get("entities") or {}):
        for m_item in holder.get("media", []) or []:
            if m_item.get("type") == "photo":
                out.append(m_item)
    entities = tweet_obj.get("entities", {}) or {}
    media_list = entities.get("media", []) or []
    for m_item in media_list:
        if m_item.get("type") == "photo":
            out.append(m_item)
    extended = tweet_obj.get("extended_entities", {}) or {}
    for m_item in extended.get("media", []) or []:
        if m_item.get("type") == "photo":
            out.append(m_item)
    # Dedup by media_key / url — sometimes entities + extended_entities overlap.
    seen = set()
    deduped = []
    for it in out:
        key = it.get("media_url_https") or it.get("media_url") or it.get("id_str")
        if key and key not in seen:
            seen.add(key)
            deduped.append(it)
    return deduped


# ---------- XAA (X Activity API, "webhook v2") payload support ----------
#
# XAA is the replacement for the Account Activity API (AAA EOL 2027-01-31) and uses
# the SAME webhook, CRC and signature scheme, but a different payload:
#
#   AAA  {"for_user_id": "...", "tweet_create_events": [ {v1.1 tweet object} ]}
#   XAA  {"data": {"event_uuid": "...", "filter": {"user_id": "..."},
#                  "event_type": "post.create", "payload": {v2 Post object},
#                  "includes": {"users": [...], "media": [...]}}}
#
# We flatten XAA post.create into the v1.1-ish dict the rest of this file already
# understands, so there is exactly one filter/mirror path for both formats.
# https://docs.x.com/x-api/activity/introduction | /event-payloads

XAA_POST_CREATE = "post.create"


def is_xaa_payload(payload: dict) -> bool:
    """True for the XAA envelope: a top-level `data` object carrying `event_type`."""
    data = payload.get("data")
    return isinstance(data, dict) and isinstance(data.get("event_type"), str)


def _first_nonempty_list(*values: Any) -> list:
    for v in values:
        if isinstance(v, list) and v:
            return v
    return []


def _xaa_referenced_posts(post: dict) -> list[dict]:
    """The canonical schema says `referenced_posts`; every sample payload uses
    `referenced_tweets`. Accept both."""
    return _first_nonempty_list(post.get("referenced_posts"), post.get("referenced_tweets"))


def _xaa_includes_index(includes: dict) -> tuple[dict[str, dict], dict[str, dict]]:
    media_by_key: dict[str, dict] = {}
    for item in _first_nonempty_list(includes.get("media")):
        if isinstance(item, dict) and item.get("media_key"):
            media_by_key[str(item["media_key"])] = item
    users_by_id: dict[str, dict] = {}
    for item in _first_nonempty_list(includes.get("users")):
        if isinstance(item, dict) and item.get("id"):
            users_by_id[str(item["id"])] = item
    return media_by_key, users_by_id


def _xaa_media_to_v11(media_obj: dict) -> dict:
    """Map a v2 media object onto the v1.1-ish shape extract_tweet_photos() reads.

    v2 photos carry the downloadable https URL in `url` (pbs.twimg.com);
    `preview_image_url` is a thumbnail used for video/gif.
    """
    out: dict[str, Any] = {
        "type": media_obj.get("type") or "photo",
        "id_str": str(media_obj.get("media_key") or ""),
    }
    url = media_obj.get("url")
    if url:
        out["media_url_https"] = url
    return out


def xaa_event_to_tweet_obj(event: dict) -> dict | None:
    """Flatten one XAA `post.create` event into the v1.1-ish tweet dict.

    Returns None for XAA event types we do not mirror (likes, follows, DMs, ...).
    """
    if event.get("event_type") != XAA_POST_CREATE:
        return None
    post = event.get("payload")
    if not isinstance(post, dict):
        return None

    includes = event.get("includes") or {}
    if not isinstance(includes, dict):
        includes = {}
    media_by_key, users_by_id = _xaa_includes_index(includes)

    author_id = str(post.get("author_id") or "")
    author = users_by_id.get(author_id) or {}

    refs = [r for r in _xaa_referenced_posts(post) if isinstance(r, dict)]
    ref_types = {str(r.get("type") or "") for r in refs}

    # v2 spelling varies: in_reply_to_tweet_id in samples, in_reply_to_post_id in schema.
    in_reply_to = (post.get("in_reply_to_tweet_id")
                   or post.get("in_reply_to_post_id")
                   or post.get("in_reply_to_status_id_str"))

    media_list: list[dict] = []
    attachments = post.get("attachments") or {}
    if isinstance(attachments, dict):
        for key in attachments.get("media_keys") or []:
            m = media_by_key.get(str(key))
            if m:
                media_list.append(_xaa_media_to_v11(m))

    # Long Posts: `note_post` (current docs) carries the full untruncated text.
    note = post.get("note_post") or post.get("note_tweet") or {}
    full_text = note.get("text") if isinstance(note, dict) else None

    out: dict[str, Any] = {
        "id_str": str(post.get("id") or ""),
        "text": post.get("text") or "",
        "display_text_range": post.get("display_text_range"),
        "entities": {"media": [dict(item) for item in media_list]},
        "referenced_tweets": refs,
        "in_reply_to_status_id_str": in_reply_to,
        "is_quote_status": "quoted" in ref_types,
        "user": {"id_str": author_id, "screen_name": author.get("username") or ""},
        # Carry the filter user through so the shared for_user_id guard still runs.
        "for_user_id": str((event.get("filter") or {}).get("user_id") or ""),
    }
    if "retweeted" in ref_types:
        # is_original_tweet() refuses anything with a truthy retweeted_status.
        out["retweeted_status"] = {"id_str": ""}
    if full_text:
        out["extended_tweet"] = {
            "full_text": full_text,
            "entities": {"media": [dict(item) for item in media_list]},
        }
        if isinstance(post.get("display_text_range"), list):
            out["extended_tweet"]["display_text_range"] = post["display_text_range"]
    return out


def extract_candidate_tweets(payload: dict, logger: logging.Logger) -> list[dict]:
    """Pull mirrorable tweet-shaped dicts out of EITHER payload format."""
    if is_xaa_payload(payload):
        data = payload["data"]
        if data.get("event_type") != XAA_POST_CREATE:
            logger.info(f"ignoring XAA event_type={data.get('event_type')}")
            return []
        event_id = str(data.get("event_uuid") or "")
        tweet = xaa_event_to_tweet_obj(data)
        if not tweet:
            logger.warning(f"XAA post.create event {event_id or '?'} had no usable payload")
            return []
        logger.info(f"XAA post.create event_uuid={event_id or '?'} post_id={tweet.get('id_str') or '?'}")
        return [tweet]

    candidates: list[dict] = []
    for event_type, events_list in payload.items():
        if event_type != "tweet_create_events":
            logger.debug(f"ignoring event_type={event_type}")
            continue
        for evt in events_list or []:
            if not isinstance(evt, dict):
                continue
            candidates.append(evt.get("tweet") if isinstance(evt.get("tweet"), dict) else evt)
    return candidates


# ---------- the mirror dispatcher ----------

def mirror_one_tweet(
    tweet_obj: dict,
    cfg: dict,
    conn: sqlite3.Connection,
    logger: logging.Logger,
    threads_post_dir: Path,
    cache_dir: Path,
) -> dict:
    """Mirror a single tweet to Threads. Used by both the webhook handler and any
    on-demand CLI mode (--replay <tweet_id>)."""
    tweet_id = str(tweet_obj.get("id_str") or tweet_obj.get("id"))
    # tweet_id becomes part of a file path below; a real one is all digits.
    if not tweet_id.isdigit():
        logger.warning("[SKIP bad-id] tweet id is not numeric")
        return {"skipped": "bad-id"}
    text = tweet_text(tweet_obj)
    handle = cfg.get("x_user_handle") or "?"
    source_url = f"https://x.com/{handle}/status/{tweet_id}"

    if m.is_already_mirrored(conn, tweet_id):
        logger.info(f"[SKIP already-mirrored] tweet {tweet_id}")
        return {"tweet_id": tweet_id, "skipped": "already-mirrored"}

    photos = extract_tweet_photos(tweet_obj)
    if not photos and not text:
        logger.info(f"[SKIP empty] tweet {tweet_id}")
        return {"tweet_id": tweet_id, "skipped": "empty"}

    # Download photos (skip if no photos — text-only is fine).
    local_photos: list[Path] = []
    try:
        for idx, photo in enumerate(photos):
            url = photo.get("media_url_https") or photo.get("media_url")
            if not url:
                continue
            if not is_allowed_media_url(url):
                raise ValueError("media url is not on X's image CDN")
            ext = ".jpg"
            if ".png" in url.lower():
                ext = ".png"
            elif ".webp" in url.lower():
                ext = ".webp"
            dest = cache_dir / f"{tweet_id}-{idx}{ext}"
            m.download_media(url, dest, logger)
            local_photos.append(dest)
    except Exception as e:
        logger.warning(f"[ERR download] tweet {tweet_id}: {e}")
        m.record_mirror(conn, tweet_id, None, None, source_url, f"download error: {e}")
        return {"tweet_id": tweet_id, "error": f"download: {e}"}

    # Posts to the single eligible Media: text + image in one container.
    # Threads supports multi-image carousels via the threads-post skill but
    # that's not yet wired; for now we send just the first photo.
    image_path = local_photos[0] if local_photos else None
    try:
        # run_threads_post accepts the same args whether called from CLI tick
        # or here; we always pass dry_run=False since this is a real event.
        observe = bool((cfg.get("webhook") or {}).get("observe_only"))
        # 1 photo -> single image post; 2+ -> Threads carousel (X's multi-image post).
        logger.info(f"mirroring tweet {tweet_id} with {len(local_photos)} photo(s)")
        result = m.run_threads_post(
            cfg, text,
            local_photos[0] if len(local_photos) == 1 else None,
            dry_run=observe, logger=logger,
            image_paths=local_photos,
        )
        if observe:
            # Not recorded in state.db, so the same post can still be mirrored for real later.
            logger.info(f"[OBSERVE] would post tweet {tweet_id}: text={text[:80]!r} image={'yes' if image_path else 'no'}")
            return {"tweet_id": tweet_id, "observed": True}
        post_id = (result.get("published") or {}).get("id") or result.get("container_id")
        post_url = result.get("post_url")
        m.record_mirror(conn, tweet_id, post_id, post_url, source_url, None)
        logger.info(f"[OK] mirrored tweet {tweet_id} → threads {post_url or post_id}")
        return {"tweet_id": tweet_id, "threads_post_id": post_id, "threads_post_url": post_url}
    except Exception as e:
        logger.warning(f"[ERR post] tweet {tweet_id}: {e}")
        m.record_mirror(conn, tweet_id, None, None, source_url, f"post error: {e}")
        return {"tweet_id": tweet_id, "error": f"post: {e}"}


# ---------- FastAPI app ----------

def build_app(
    cfg: dict,
    consumer_secret: str | None,
    oauth2_secret: str | None,
    conn: sqlite3.Connection,
    logger: logging.Logger,
) -> FastAPI:
    threads_post_dir = Path(cfg.get("env_dir") or HERE)
    cache_dir = HERE / cfg["media_cache_dir"]
    cache_dir.mkdir(parents=True, exist_ok=True)
    x_user_id = int(cfg.get("x_user_id") or 0)
    handle = cfg.get("x_user_handle") or "?"

    # CRC response_token is keyed with the OAuth 2.0 client secret when the app has
    # one; the OAuth 1.0 consumer secret is only the documented fallback for apps
    # that have no OAuth 2.0 secret at all. X_WEBHOOK_CRC_KEY=legacy|oauth2 pins it.
    crc_secret, crc_secret_source = select_crc_secret(
        oauth2_secret, consumer_secret, cfg.get("webhook_crc_key") or "auto"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("webhook listener ready")
        yield
        try:
            conn.close()
            logger.info("sqlite connection closed")
        except Exception:
            pass

    app = FastAPI(title="x-threads-mirror webhook", docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # ---- Threads OAuth callback -------------------------------------------
    # The Threads app needs a registered redirect URI. Using a fresh
    # trycloudflare quick-tunnel URL means re-registering a new URL in the Meta
    # dashboard on every re-auth. Instead we register ONE permanent URI:
    #
    #   https://listener.example.com/oauth/callback
    #
    # and guard it with a single-use nonce in the path:
    #
    #   https://<your-public-host>/oauth/callback/<nonce>
    #
    # The nonce is written to oauth_nonce.txt in this directory by whatever tool
    # drives the public-redirect flow. Without the correct nonce the route refuses,
    # so this endpoint cannot be used to hijack an OAuth code.
    @app.get("/oauth/callback/{nonce}")
    async def oauth_callback(request: Request, nonce: str):
        expected = ""
        try:
            expected = (HERE / "oauth_nonce.txt").read_text().strip()
        except Exception:
            expected = ""
        if not expected or not hmac.compare_digest(expected, nonce):
            logger.warning(f"OAuth callback rejected: bad nonce from {request.client.host if request.client else '?'}")
            return JSONResponse({"error": "invalid nonce"}, status_code=403)

        code = request.query_params.get("code")
        err = request.query_params.get("error")
        if err:
            desc = request.query_params.get("error_description", "")
            logger.warning(f"OAuth callback error: {err} {desc}")
            try:
                (HERE / "oauth_nonce.txt").unlink(missing_ok=True)
            except Exception:
                pass
            return JSONResponse({"error": err, "error_description": desc}, status_code=400)

        if not code:
            return JSONResponse({"error": "missing code"}, status_code=400)

        # Persist the code for the driver tool to pick up.
        (HERE / "oauth_code.txt").write_text(code)
        # Single-use: burn the nonce so the same URL cannot be replayed.
        try:
            (HERE / "oauth_nonce.txt").unlink(missing_ok=True)
        except Exception:
            pass
        logger.info(f"OAuth callback accepted (nonce {nonce[:8]}…); code captured for exchange")
        return {
            "status": "ok",
            "message": "Authorization received. You can close this tab and return to the terminal.",
        }

    # X calls this GET on registration, on PUT /2/webhooks/{id}, and periodically as
    # a liveness ping. Reply with an HMAC-signed response_token so X knows we hold
    # the app secret. X keys this token with the OAuth 2.0 client secret when the app
    # has one, and only falls back to the OAuth 1.0 consumer secret for apps that have
    # no OAuth 2.0 secret at all — so prefer the client secret here too.
    @app.get("/webhooks/twitter")
    async def crc(crc_token: str):
        if not CRC_TOKEN_RE.fullmatch(crc_token):
            logger.warning(f"CRC rejected: token is not a plausible crc_token (len={len(crc_token)})")
            return JSONResponse({"error": "invalid crc_token"}, status_code=400)
        if not crc_secret:
            logger.error("CRC failed: no signing secret configured")
            return JSONResponse({"error": "no signing secret"}, status_code=500)
        response_token = compute_signature(crc_token.encode("utf-8"), crc_secret)
        logger.info(f"CRC handshake (token len={len(crc_token)}, secret={crc_secret_source})")
        return {"response_token": response_token}

    @app.post("/webhooks/twitter")
    async def events(request: Request):
        raw_body = await request.body()
        sender = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
        agent = (request.headers.get("user-agent") or "?")[:60]
        # Header NAMES only — never values. This is what tells us which credential
        # scheme X is actually using on a delivery or a rejection.
        sig_headers = [h for h in (SIG_HEADER_OAUTH2, SIG_HEADER_LEGACY) if request.headers.get(h)]

        # HMAC verify against whichever signature scheme X used (timing-safe).
        ok, matched_header = verify_signature(raw_body, request.headers, oauth2_secret, consumer_secret)
        if not ok:
            logger.warning(
                f"POST rejected: invalid or missing signature (from {sender}, ua={agent}, "
                f"{len(raw_body)} bytes, signature headers present={sig_headers or 'none'})"
            )
            return JSONResponse({"error": "invalid signature"}, status_code=401)

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as e:
            logger.warning(f"POST rejected: invalid JSON: {e}")
            return JSONResponse({"error": "invalid json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "invalid payload"}, status_code=400)
        # Key names only, never content. Without this, a delivery that carries no
        # tweet_create_events (likes, follows, DMs) is invisible in the log.
        logger.info(f"POST accepted from {sender} ua={agent} sig={matched_header}: event types={sorted(payload.keys())}")
        # Count each event list. An empty tweet_create_events array is a real X
        # failure mode (seen 2026-11-09 10:48:20 ET: a delivery was attempted and
        # carried zero events inside) and is otherwise invisible, because nothing
        # ever iterates an empty list.
        counts = {k: (len(v) if isinstance(v, list) else "scalar") for k, v in payload.items()}
        logger.info(f"event counts: {counts}")

        # Either payload format lands here:
        #   AAA  {"for_user_id": "...", "tweet_create_events": [ {v1.1 tweet} ]}
        #   XAA  {"data": {"event_type": "post.create", "payload": {v2 Post}, ...}}
        # extract_candidate_tweets() flattens both to the v1.1-ish shape the filters
        # below (and mirror_one_tweet) already understand.
        results = []
        for evt in extract_candidate_tweets(payload, logger):
            for_user_id = str(evt.get("for_user_id") or "")
            if x_user_id and for_user_id and for_user_id != str(x_user_id):
                # Event for a different subscribed user; ignore.
                logger.info(f"ignoring event for for_user_id={for_user_id} (not our {x_user_id})")
                continue

            # X wraps the actual tweet inside the event under "tweet" or as the event itself.
            # Old AAA API: {"for_user_id": "...", "tweet_create_events": [{...tweet...}]}
            # Where {...tweet...} IS the tweet object.
            tweet_obj = evt.get("tweet") if isinstance(evt.get("tweet"), dict) else evt
            # Shape only (key names, never content): if X's payload ever differs
            # from what the filters below expect, this line shows how.
            logger.info(f"event shape: keys={sorted(tweet_obj.keys())[:30]} user_keys={sorted((tweet_obj.get('user') or {}).keys())[:8]}")

            # Author check, fail closed: X also delivers tweet_create_events for
            # OTHER people's tweets that mention or reply to this account. Only a
            # tweet whose author is the configured user is ever mirrored; if the
            # author is missing from the payload, nothing is posted.
            author_id = str(((tweet_obj.get("user") or {}).get("id_str"))
                            or (tweet_obj.get("user") or {}).get("id")
                            or tweet_obj.get("author_id") or "")
            if not x_user_id or author_id != str(x_user_id):
                tweet_id = str(tweet_obj.get("id_str") or tweet_obj.get("id"))
                logger.info(f"[SKIP not-author] tweet {tweet_id} author={author_id or '?'}")
                results.append({"tweet_id": tweet_id, "skipped": "not-author"})
                continue

            # Quote posts: a repost with a comment. Copied to Threads they become a few
            # words and a t.co link, so they are skipped unless the operator opts in.
            is_quote = bool(tweet_obj.get("is_quote_status") or tweet_obj.get("quoted_status")
                            or any((r or {}).get("type") == "quoted" for r in (tweet_obj.get("referenced_tweets") or [])))
            if is_quote and not (cfg.get("webhook") or {}).get("mirror_quote_posts"):
                tweet_id = str(tweet_obj.get("id_str") or tweet_obj.get("id"))
                logger.info(f"[SKIP quote] tweet {tweet_id}")
                results.append({"tweet_id": tweet_id, "skipped": "quote"})
                continue

            ok, reason = is_original_tweet(tweet_obj)
            if not ok:
                tweet_id = str(tweet_obj.get("id_str") or tweet_obj.get("id"))
                logger.info(f"[SKIP {reason}] tweet {tweet_id}")
                results.append({"tweet_id": tweet_id, "skipped": reason})
                continue

            # Video-only check: if any media is video/animation, skip the whole tweet
            # (we don't handle video). Text-only with no media is fine.
            photo_count = len(extract_tweet_photos(tweet_obj))
            _ext = tweet_obj.get("extended_tweet") or {}
            has_video = any(
                (it.get("type") in ("video", "animated_gif"))
                for it in (_ext.get("extended_entities", {}) or {}).get("media", [])
            ) or any(
                (it.get("type") in ("video", "animated_gif"))
                for it in (tweet_obj.get("extended_entities", {}) or {}).get("media", [])
            ) or any(
                (it.get("type") in ("video", "animated_gif"))
                for it in (tweet_obj.get("entities", {}) or {}).get("media", [])
            )
            if has_video and photo_count == 0:
                tweet_id = str(tweet_obj.get("id_str") or tweet_obj.get("id"))
                logger.info(f"[SKIP video-only] tweet {tweet_id}")
                results.append({"tweet_id": tweet_id, "skipped": "video-only"})
                continue

            r = mirror_one_tweet(
                tweet_obj, cfg, conn, logger,
                threads_post_dir=threads_post_dir,
                cache_dir=cache_dir,
            )
            results.append(r)

        return {"processed": results}

    return app


# ---------- main ----------

def main() -> int:
    p = argparse.ArgumentParser(
        description="Stream-only X→Threads mirror via Account Activity webhook."
    )
    p.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1 — tunneled via cloudflared)")
    p.add_argument("--port", type=int, default=8644, help="Bind port (default 8644)")
    p.add_argument("--config", default=str(CONFIG_PATH))
    p.add_argument("--log-file", help="Override log file path")
    args = p.parse_args()

    cfg = load_config(Path(args.config))

    log_path = Path(args.log_file) if args.log_file else HERE / cfg["log_file"]
    logger = make_logger(log_path, int(cfg["log_max_bytes"]), int(cfg["log_backup_count"]))
    logger.info(f"x-threads-mirror-webhook starting on {args.host}:{args.port}")

    # HMAC keys for webhook verification and CRC.
    #   consumer_secret = X OAuth 1.0a consumer secret (legacy signature header)
    #   oauth2_secret   = X OAuth 2.0 client secret   (preferred by the current docs)
    env_dir = Path(cfg.get("env_dir") or HERE)
    x_creds = m.load_x_creds(env_dir)  # (consumer_key, consumer_secret, access_token, access_token_secret)
    consumer_secret = x_creds[1]
    oauth2_secret = load_oauth2_client_secret(env_dir)
    logger.info(
        "webhook signing secrets: oauth2_client_secret=%s legacy_consumer_secret=%s"
        % ("configured" if oauth2_secret else "MISSING", "configured" if consumer_secret else "MISSING")
    )
    if not oauth2_secret:
        logger.warning(
            "No X OAuth 2.0 client secret configured. Deliveries signed ONLY with "
            "X-Twitter-Webhooks-Signature-OAuth2 will be rejected and CRC will be keyed "
            "with the OAuth 1.0 consumer secret. To enable the OAuth 2.0 path, add "
            "X_OAUTH2_CLIENT_SECRET=<app OAuth 2.0 Client Secret> to %s"
            % (env_dir / ".env")
        )
    if not (oauth2_secret or consumer_secret):
        logger.error("No webhook signing secret at all; every CRC and POST will fail")

    # Verify Threads creds loaded (used inside mirror_one_tweet via subprocess).
    m.load_threads_creds(env_dir)

    # SQLite state (shared DB with the legacy mirror.py for idempotency).
    conn = m.init_state(HERE / cfg["state_db_path"])

    # If x_user_id isn't pinned in config, resolve it from the API.
    if not cfg.get("x_user_id"):
        import tweepy
        x_client = m.make_x_client(x_creds[:4])
        uid = m.resolve_user_id(x_client, cfg.get("x_user_handle") or "")
        cfg["x_user_id"] = uid
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
        logger.info(f"resolved @{cfg.get('x_user_handle')} → user_id={uid}")

    app = build_app(cfg, consumer_secret, oauth2_secret, conn, logger)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)