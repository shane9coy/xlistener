#!/usr/bin/env python3
"""post_to_threads.py — Post text/image/video to your Threads account.

Meta's Threads API cURLs your `image_url` server-side, so the URL MUST be
publicly reachable. Local file paths do NOT work — Meta will respond with
"unknown error" / OAuthException. Use `--image` for a local file (auto-hosted)
or `--image-url` if you've already uploaded it somewhere.

Required env (in the repo-root .env, next to this script; override the path with $THREADS_POST_ENV):
  THREADS_ACCESS_TOKEN   — 60-day long-lived Threads user token
  THREADS_USER_ID        — numeric Threads user ID

Usage:
  # Text post (single)
  post_to_threads.py "Hello from the new skill."

  # Image post (single) — local file auto-hosted
  post_to_threads.py "Caption here." --image /path/to/img.jpg

  # Image post — public URL you already have
  post_to_threads.py "Caption." --image-url https://example.com/img.jpg

  # 2-post thread: image, then text reply (most common for a picture + comment)
  post_to_threads.py --thread "🛡️" --image /path/to/img.jpg

  # 2-post thread: text, then image reply
  post_to_threads.py --thread-text-first "Look at this" --image /path/to/img.jpg

  # Reply to an existing post
  post_to_threads.py --reply-to <post_id> "Quick reply."

  # Dry-run (prints container payloads, doesn't POST)
  post_to_threads.py --dry-run "Preview only."

This module is a CLI. The webhook listener runs it as a subprocess
(`mirror.run_threads_post`) rather than importing it, so a publish failure can never
take the listener down. Imports from tests are for unit coverage only.

Reference: https://developers.facebook.com/documentation/threads/posts.md
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# Env resolution:
#   1. $THREADS_POST_ENV override (set to a .env path)
#   2. <repo>/.env  (next to this script)
# Loaded lazily and kept in a private dict: the values are NOT exported into
# os.environ, so a wrapped or compromised child (curl) cannot inherit the X and
# Threads credentials.
HOME = Path.home()
REPO_DIR = Path(__file__).resolve().parent
_ENV_CACHE: dict[str, str] | None = None


def _env_file() -> Path:
    override = os.environ.get("THREADS_POST_ENV", "").strip()
    if override and Path(override).exists():
        return Path(override)
    return REPO_DIR / ".env"


def _load_env() -> dict[str, str]:
    global _ENV_CACHE
    if _ENV_CACHE is None:
        _ENV_CACHE = {}
        path = _env_file()
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                _ENV_CACHE[k.strip()] = v.strip().strip('"').strip("'")
    return _ENV_CACHE


class ThreadsPostError(SystemExit):
    """A user-actionable failure.

    Subclasses SystemExit so the CLI still exits with the message, while library and
    test callers can catch it instead of the process dying.
    """


def _fail(message: str) -> None:
    raise ThreadsPostError(message)


THREADS_API = "https://graph.threads.net/v1.0"
CAROUSEL_MIN_ITEMS = 2
CAROUSEL_MAX_ITEMS = 20
TEXT_LIMIT = 500
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
VIDEO_TYPES = {"video/mp4", "video/quicktime"}

# Public image hosts tried in order. Each entry is (label, curl args, url_extractor).
# url_extractor takes the curl stdout and returns the public URL or "" on failure.
IMAGE_HOSTS = [
    (
        "uguu.se",
        ["curl", "-fsS", "-m", "30",
         "-F", "files[]=@{path}",
         "https://uguu.se/upload?output=text"],
        lambda out: out.strip(),
    ),
    (
        "litterbox.catbox.moe (24h)",
        ["curl", "-fsS", "-m", "30",
         "-F", "reqtype=fileupload",
         "-F", "time=24h",
         "-F", "fileToUpload=@{path}",
         "https://litterbox.catbox.moe/resources/internals/api.php"],
        lambda out: out.strip(),
    ),
    (
        "catbox.moe (permanent)",
        ["curl", "-fsS", "-m", "30",
         "-F", "reqtype=fileupload",
         "-F", "fileToUpload=@{path}",
         "https://catbox.moe/user/api.php"],
        lambda out: out.strip(),
    ),
    (
        "0x0.st (24h)",
        ["curl", "-fsS", "-m", "30",
         "-F", "file=@{path}",
         "-F", "expires=24",
         "https://0x0.st"],
        lambda out: out.strip(),
    ),
]


def _require_creds() -> tuple[str, str]:
    env = _load_env()
    token = env.get("THREADS_ACCESS_TOKEN", "").strip()
    uid = env.get("THREADS_USER_ID", "").strip()
    if not token:
        _fail("THREADS_ACCESS_TOKEN not set in .env — see tools/threads_oauth.py")
    if not uid:
        _fail("THREADS_USER_ID not set in .env — see tools/threads_oauth.py")
    return token, uid


def _form_post(url: str, data: dict, timeout: float = 30.0) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        _fail(f"POST {url} → {e.code}: {body_text}")


def _guess_mime(path: str) -> str:
    mt, _ = mimetypes.guess_type(path)
    return mt or "application/octet-stream"


# Only these hosts may be handed to Meta as the image_url. Anything else could be
# used to make Meta fetch arbitrary content (or us HEAD an internal address).
UPLOAD_HOST_ALLOWLIST = ("uguu.se", "litterbox.catbox.moe", "catbox.moe", "0x0.st")


def _is_allowed_upload_url(url: str) -> bool:
    try:
        u = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if u.scheme != "https":
        return False
    if "@" in (u.netloc or "") or "\\" in url:
        return False
    host = (u.hostname or "").lower()
    return any(host == allowed or host.endswith("." + allowed) for allowed in UPLOAD_HOST_ALLOWLIST)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D102
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _verify_public_url(url: str) -> bool:
    """HEAD the URL and check it actually returns bytes (not 0-length, not HTML).

    Redirects are refused: the allow-list must not be escapeable through a 3xx.
    """
    if not _is_allowed_upload_url(url):
        print(f"  ! URL host is not an allow-listed upload host — skipping: {url}", file=sys.stderr)
        return False
    try:
        req = urllib.request.Request(url, method="HEAD")
        with _NO_REDIRECT_OPENER.open(req, timeout=15) as resp:
            ct = resp.headers.get("Content-Type", "").lower()
            cl = resp.headers.get("Content-Length", "")
            if not ct.startswith("image/"):
                print(f"  ! URL returned Content-Type={ct}, not an image — skipping", file=sys.stderr)
                return False
            if cl == "0":
                print(f"  ! URL returned Content-Length=0 — host served empty body, skipping", file=sys.stderr)
                return False
            return True
    except Exception as e:
        print(f"  ! HEAD failed: {e}", file=sys.stderr)
        return False


def upload_to_public_host(path: str) -> str:
    """Upload a local image to a public host. Tries uguu → litterbox → catbox → 0x0.st.

    Returns the public URL. Exits on total failure.
    Verifies the result with a HEAD request (catches 0-length responses, wrong MIME, etc).
    """
    if not Path(path).exists():
        _fail(f"Image not found: {path}")
    mime = _guess_mime(path)
    if mime not in IMAGE_TYPES:
        _fail(f"Unsupported image type: {mime}. Use jpg/png/webp.")

    print(f"Uploading {path} to a public host (Meta API will cURL this URL)…", file=sys.stderr)
    for label, args, extractor in IMAGE_HOSTS:
        print(f"  → trying {label}…", end="", flush=True, file=sys.stderr)
        cmd = [a.replace("{path}", path) for a in args]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
            if result.returncode != 0:
                print(f" curl failed (exit {result.returncode})", file=sys.stderr)
                continue
            url = extractor(result.stdout)
            if not url or not _is_allowed_upload_url(url):
                print(f" no allow-listed URL in response: {result.stdout!r}", file=sys.stderr)
                continue
            # Sanity-check the URL
            if not _verify_public_url(url):
                print(f"    URL failed verification: {url}", file=sys.stderr)
                continue
            print(f" ok → {url}", file=sys.stderr)
            return url
        except subprocess.TimeoutExpired:
            print(" timeout", file=sys.stderr)
        except Exception as e:
            print(f" error: {e}", file=sys.stderr)

    _fail("All public image hosts failed. Try --image-url with a URL you uploaded manually.")


def _create_text_container(token: str, user_id: str, text: str, reply_to: str | None) -> str:
    if len(text) > TEXT_LIMIT:
        _fail(f"Text too long: {len(text)} chars (Threads limit is {TEXT_LIMIT})")
    payload = {
        "media_type": "TEXT",
        "text": text,
        "access_token": token,
    }
    if reply_to:
        payload["reply_to_id"] = reply_to
    resp = _form_post(f"{THREADS_API}/{user_id}/threads", payload)
    cid = resp.get("id")
    if not cid:
        _fail(f"No container id in response: {resp}")
    return cid


def _create_image_container(
    token: str, user_id: str, text: str, image_url: str,
    reply_to: str | None, is_carousel_item: bool = False,
) -> str:
    """Create an image media container.

    `image_url` MUST be a publicly reachable URL. Meta's server will cURL it.
    If you have a local file, call `upload_to_public_host` first.
    """
    payload = {
        "media_type": "IMAGE",
        "image_url": image_url,
        "is_carousel_item": str(is_carousel_item).lower(),
        "access_token": token,
    }
    if text and not is_carousel_item:
        payload["text"] = text
    if reply_to and not is_carousel_item:
        payload["reply_to_id"] = reply_to
    resp = _form_post(f"{THREADS_API}/{user_id}/threads", payload)
    cid = resp.get("id")
    if not cid:
        _fail(f"No container id in response: {resp}")
    return cid


def _create_carousel_parent(
    token: str, user_id: str, text: str, child_ids: list[str], reply_to: str | None = None,
) -> str:
    """Create the parent CAROUSEL container that lists already-created child containers.

    Threads carousels are two levels: each item is its own IMAGE container created with
    `is_carousel_item=true` (and NO text), then a parent with `media_type=CAROUSEL` whose
    `children` is the comma-separated list of those child container ids. The caption lives
    on the parent only.
    """
    if len(child_ids) < CAROUSEL_MIN_ITEMS:
        _fail(f"Carousel needs at least {CAROUSEL_MIN_ITEMS} items, got {len(child_ids)}")
    if len(child_ids) > CAROUSEL_MAX_ITEMS:
        _fail(f"Carousel allows at most {CAROUSEL_MAX_ITEMS} items, got {len(child_ids)}")
    payload = {
        "media_type": "CAROUSEL",
        "children": ",".join(child_ids),
        "access_token": token,
    }
    if text:
        if len(text) > TEXT_LIMIT:
            _fail(f"Caption too long: {len(text)} chars (limit {TEXT_LIMIT})")
        payload["text"] = text
    if reply_to:
        payload["reply_to_id"] = reply_to
    resp = _form_post(f"{THREADS_API}/{user_id}/threads", payload)
    cid = resp.get("id")
    if not cid:
        _fail(f"No carousel container id in response: {resp}")
    return cid


def _publish(token: str, user_id: str, creation_id: str) -> dict:
    """Publish a container, retrying while Threads still says it does not exist.

    A container is not always publishable the instant it is created. Until it is,
    threads_publish answers 400 / code 24 / subcode 4279009 "Media Not Found".
    That is a race, not a failure, so wait and try again (about a minute in total).
    """
    url = f"{THREADS_API}/{user_id}/threads_publish"
    body = urllib.parse.urlencode({"creation_id": creation_id, "access_token": token}).encode()
    delay = 2.0
    for attempt in range(1, 4):
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode(errors="replace")
            not_ready = e.code == 400 and "4279009" in body_text
            if not_ready and attempt < 9:
                print(f"  container {creation_id} not ready (attempt {attempt}), retrying in {delay:.0f}s", file=sys.stderr)
                time.sleep(delay)
                delay = min(delay * 1.5, 6.0)
                continue
            _fail(f"POST {url} → {e.code}: {body_text}")
    _fail(f"POST {url}: container {creation_id} never became publishable")


def _verify_published(token: str, post_id: str, timeout: int = 8) -> dict | None:
    """The Threads publish endpoint can return a 200 with a numeric ID even when
    the post is silently invalid (we've seen this: permalink 302s to
    `?error=invalid_post`, GET returns "does not exist"). The signal that a post
    is actually live is: GET on the post ID returns a body with a `permalink`
    field, and the permalink URL follows a 200, not a 302 to invalid_post.

    This helper polls the GET endpoint for up to `timeout` seconds and returns
    the verified post object, or None if it never becomes live.
    """
    deadline = time.time() + timeout
    get_url = f"{THREADS_API}/{post_id}?fields=id,media_type,permalink,is_reply,root_post&access_token={token}"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(get_url, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            if data.get("permalink") and "error=invalid_post" not in data["permalink"]:
                return data
        except urllib.error.HTTPError as e:
            if e.code != 100:  # 100 = "does not exist", expected during async processing
                pass
        except Exception:
            pass
        time.sleep(2)
    return None


def _publish_with_verify(
    token: str, user_id: str, creation_id: str,
    recreate_fn=None, max_retries: int = 1,
) -> dict:
    """Publish a container and verify the result is actually live. Retry on silent
    failure (publish returns success but post is invalid). `recreate_fn` is a
    zero-arg callable that returns a new creation_id, used between attempts.
    Returns the verified post object. Exits if all attempts fail.
    """
    last_post_id = None
    for attempt in range(1, max_retries + 2):  # initial + N retries
        pub = _publish(token, user_id, creation_id)
        post_id = pub.get("id")
        if not post_id:
            _fail(f"Publish attempt {attempt}: no id in response: {pub}")
        last_post_id = post_id
        print(f"  publish attempt {attempt}: id={post_id}", file=sys.stderr)
        verified = _verify_published(token, post_id, timeout=8)
        if verified:
            print(f"  → verified live: {verified.get('permalink')}", file=sys.stderr)
            return verified
        if attempt <= max_retries:
            print(f"  → publish returned ID but post not live (silent fail). retrying…", file=sys.stderr)
            if recreate_fn is None:
                break
            creation_id = recreate_fn()
            if not creation_id:
                break
            _wait_for_image_ready(token, user_id, creation_id)
        else:
            print(f"  → still not live after {attempt} attempts.", file=sys.stderr)
    _fail(f"Publish failed verification after {max_retries + 1} attempts. Last id: {last_post_id}")


def _wait_for_image_ready(token: str, user_id: str, creation_id: str, max_wait: int = 20) -> None:
    """Poll the container status until FINISHED or error. The doc says wait 30s on average
    for image processing; we cap at max_wait seconds."""
    deadline = time.time() + max_wait
    status_url = f"{THREADS_API}/{creation_id}?fields=status,error_message&access_token={token}"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(status_url, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            status = data.get("status", "")
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                _fail(f"Container {creation_id} failed: status={status}, error={data.get('error_message')}")
        except Exception:
            pass
        time.sleep(3)


def post_text(text: str, reply_to: str | None = None, dry_run: bool = False) -> dict:
    token, uid = _require_creds()
    payload = {
        "media_type": "TEXT",
        "text": text,
        "access_token": "<token>" if dry_run else token,
    }
    if reply_to:
        payload["reply_to_id"] = reply_to
    if dry_run:
        return {
            "dry_run": True,
            "step1_create": f"{THREADS_API}/{uid}/threads",
            "step1_payload": payload,
            "step2_publish": f"{THREADS_API}/{uid}/threads_publish",
        }
    cid = _create_text_container(token, uid, text, reply_to)
    # Text containers need the same readiness wait as image ones before publishing.
    _wait_for_image_ready(token, uid, cid, max_wait=30)
    pub = _publish_with_verify(
        token, uid, cid,
        recreate_fn=lambda: _create_text_container(token, uid, text, reply_to),
    )
    return {
        "container_id": cid,
        "published": pub,
        "post_url": pub.get("permalink", f"https://www.threads.net/@/{pub.get('id', '')}"),
    }


def post_image(
    text: str, image_path: str | None, image_url: str | None,
    reply_to: str | None = None, dry_run: bool = False,
) -> dict:
    token, uid = _require_creds()
    if image_path and image_url:
        _fail("Pass either --image or --image-url, not both.")
    if not image_path and not image_url:
        _fail("Image post requires --image <path> or --image-url <url>.")
    if text and len(text) > TEXT_LIMIT:
        _fail(f"Caption too long: {len(text)} chars (limit {TEXT_LIMIT})")
    if image_path and not Path(image_path).exists():
        _fail(f"Image not found: {image_path}")

    # A dry run must not upload anything: observe mode is the documented "publish
    # nothing" gate, and uploading still leaks the media to a third-party host.
    if dry_run:
        return {
            "dry_run": True,
            "step1_create": f"{THREADS_API}/{uid}/threads",
            "step1_payload": {
                "media_type": "IMAGE",
                "image_url": image_url or "<local file, uploaded on a real run>",
                "is_carousel_item": "false",
                "text": text,
                "access_token": "<token>",
            },
            "step2_publish": f"{THREADS_API}/{uid}/threads_publish",
        }

    if image_path:
        image_url = upload_to_public_host(image_path)

    payload = {
        "media_type": "IMAGE",
        "image_url": image_url,
        "is_carousel_item": "false",
        "access_token": token,
    }
    if text:
        payload["text"] = text
    if reply_to:
        payload["reply_to_id"] = reply_to
    cid = _create_image_container(token, uid, text, image_url, reply_to)
    _wait_for_image_ready(token, uid, cid)
    pub = _publish_with_verify(
        token, uid, cid,
        recreate_fn=lambda: _create_image_container(token, uid, text, image_url, reply_to),
    )
    return {
        "container_id": cid,
        "image_url": image_url,
        "published": pub,
        "post_url": pub.get("permalink", f"https://www.threads.net/@/{pub.get('id', '')}"),
    }


def post_carousel(
    text: str,
    image_paths: list[str] | None = None,
    image_urls: list[str] | None = None,
    reply_to: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Post a multi-image carousel (X's "multiple image" post) to Threads.

    Each image needs a public URL because Meta cURLs it server-side, so local paths are
    auto-hosted first. Carousel = 2..20 items; X itself caps a post at 4 images.
    """
    token, uid = _require_creds()
    paths = list(image_paths or [])
    urls = list(image_urls or [])
    if paths and urls:
        _fail("Pass either --images or --image-urls, not both.")
    total = len(paths) + len(urls)
    if total < CAROUSEL_MIN_ITEMS:
        _fail(f"Carousel needs at least {CAROUSEL_MIN_ITEMS} images, got {total}")
    if total > CAROUSEL_MAX_ITEMS:
        _fail(f"Carousel allows at most {CAROUSEL_MAX_ITEMS} images, got {total}")
    if text and len(text) > TEXT_LIMIT:
        _fail(f"Caption too long: {len(text)} chars (limit {TEXT_LIMIT})")

    child_payload = {
        "media_type": "IMAGE",
        "is_carousel_item": "true",
        "access_token": "<token>",
    }
    parent_payload = {
        "media_type": "CAROUSEL",
        "children": "<child_id_1>,<child_id_2>...",
        "text": text,
        "access_token": "<token>",
    }

    if dry_run:
        return {
            "dry_run": True,
            "carousel_items": total,
            "steps": [
                {
                    "step": "create_child_container (once per image)",
                    "endpoint": f"{THREADS_API}/{uid}/threads",
                    "payload": child_payload,
                    "image_url": urls or ["<local file, auto-hosted>"] * total,
                },
                {
                    "step": "create_parent_container",
                    "endpoint": f"{THREADS_API}/{uid}/threads",
                    "payload": parent_payload,
                },
                {
                    "step": "publish",
                    "endpoint": f"{THREADS_API}/{uid}/threads_publish",
                    "payload": {"creation_id": "<parent_id>", "access_token": "<token>"},
                },
            ],
        }

    if paths:
        for path in paths:
            if not Path(path).exists():
                _fail(f"Image not found: {path}")
        urls = [upload_to_public_host(p) for p in paths]

    def _create_children() -> list[str]:
        created: list[str] = []
        for idx, url in enumerate(urls):
            print(f"  carousel item {idx + 1}/{len(urls)}: {url}", file=sys.stderr)
            cid = _create_image_container(token, uid, "", url, None, is_carousel_item=True)
            _wait_for_image_ready(token, uid, cid)
            created.append(cid)
        return created

    child_ids = _create_children()
    parent_id = _create_carousel_parent(token, uid, text, child_ids, reply_to)
    _wait_for_image_ready(token, uid, parent_id)

    def _recreate() -> str:
        """Between publish retries every container must be rebuilt, children first."""
        fresh_children = _create_children()
        fresh_parent = _create_carousel_parent(token, uid, text, fresh_children, reply_to)
        _wait_for_image_ready(token, uid, fresh_parent)
        return fresh_parent

    pub = _publish_with_verify(token, uid, parent_id, recreate_fn=_recreate)
    return {
        "container_id": parent_id,
        "child_container_ids": child_ids,
        "carousel_items": len(urls),
        "image_urls": urls,
        "published": pub,
        "post_url": pub.get("permalink", f"https://www.threads.net/@/{pub.get('id', '')}"),
    }


def post_image_then_text(
    image_path: str | None, image_url: str | None,
    reply_text: str, dry_run: bool = False,
) -> dict:
    """Post an image, then a text reply (2-post thread)."""
    p1 = post_image(text="", image_path=image_path, image_url=image_url, dry_run=dry_run)
    if dry_run:
        return {"dry_run": True, "step1_image": p1, "step2_text": "would reply to <post1>"}
    p1_post_id = p1["published"].get("id")
    p2 = post_text(text=reply_text, reply_to=p1_post_id, dry_run=dry_run)
    return {
        "post1": p1,
        "post2": p2,
        "thread_url": f"https://www.threads.net/@/{p1_post_id}",
    }


def post_text_then_image(
    lead_text: str, image_path: str | None, image_url: str | None, dry_run: bool = False,
) -> dict:
    """Post a text post, then an image reply (2-post thread)."""
    p1 = post_text(text=lead_text, dry_run=dry_run)
    if dry_run:
        return {"dry_run": True, "step1_text": p1, "step2_image": "would reply with image to <post1>"}
    p1_post_id = p1["published"].get("id")
    p2 = post_image(text="", image_path=image_path, image_url=image_url, reply_to=p1_post_id, dry_run=dry_run)
    return {
        "post1": p1,
        "post2": p2,
        "thread_url": f"https://www.threads.net/@/{p1_post_id}",
    }


def _load_text_thread_arg(raw: str) -> list[str]:
    """Resolve a --text-thread value into a list of post strings.

    Accepts:
      - "@/path/to/file.json"  -> JSON array of strings
      - a literal JSON array string (e.g. '["a","b"]')
      - "|||" delimited text, e.g. "post 1|||post 2|||post 3"
    """
    if raw.startswith("@"):
        path = Path(raw[1:]).expanduser()
        if not path.is_file():
            _fail(f"--text-thread file not found: {path}")
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            _fail(f"--text-thread file is not valid JSON: {e}")
        if not isinstance(data, list) or not all(isinstance(s, str) for s in data):
            _fail("--text-thread JSON file must be an array of strings")
        return data
    stripped = raw.strip()
    if stripped.startswith("["):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as e:
            _fail(f"--text-thread literal is not valid JSON: {e}")
        if not isinstance(data, list) or not all(isinstance(s, str) for s in data):
            _fail("--text-thread literal JSON must be an array of strings")
        return data
    return [p for p in raw.split("|||") if p.strip()]


def post_text_thread(texts: list[str], dry_run: bool = False) -> dict:
    """Post a chain of text containers, each replying to the previous.

    Hard cap: 10 posts per chain (Threads UX breaks past ~5; the cap leaves
    headroom for longer threads without inviting runaway chains).

    Each post reuses the same create+publish+verify path as `post_text`,
    so silent-invalid posts get retried automatically.

    Bails on first failure: the chain stops and the partial result is
    returned. No orphan half-threads by design — the operator can see
    exactly which post failed and decide whether to fix and continue.
    """
    MAX_CHAIN = 10
    if not texts:
        _fail("--text-thread requires at least 1 post")
    if len(texts) > MAX_CHAIN:
        _fail(f"--text-thread cap is {MAX_CHAIN} posts; got {len(texts)}")
    for i, t in enumerate(texts, 1):
        if not t.strip():
            _fail(f"--text-thread post {i} is empty")
        if len(t) > TEXT_LIMIT:
            _fail(f"--text-thread post {i} is {len(t)} chars (limit {TEXT_LIMIT})")

    token, uid = _require_creds()

    if dry_run:
        return {
            "dry_run": True,
            "chain": [
                {
                    "index": i,
                    "char_count": len(t),
                    "reply_to": "<post_{i-1}_id>" if i > 1 else None,
                    "text_preview": t[:80] + ("..." if len(t) > 80 else ""),
                }
                for i, t in enumerate(texts, 1)
            ],
            "note": "Dry-run only. Re-run without --dry-run to publish.",
        }

    results: list[dict] = []
    prev_id: str | None = None
    for i, t in enumerate(texts, 1):
        cid = _create_text_container(token, uid, t, reply_to=prev_id)
        pub = _publish_with_verify(
            token, uid, cid,
            recreate_fn=lambda t=t, prev_id=prev_id: _create_text_container(
                token, uid, t, reply_to=prev_id
            ),
        )
        prev_id = pub.get("id")
        if not prev_id:
            return {
                "failed_at_index": i,
                "partial": results,
                "error": f"post {i} published but no id returned: {pub}",
            }
        results.append({
            "index": i,
            "post_id": prev_id,
            "permalink": pub.get("permalink", f"https://www.threads.net/@/{prev_id}"),
            "char_count": len(t),
        })

    return {
        "chain_length": len(results),
        "posts": results,
        "thread_url": results[0]["permalink"] if results else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Post to your Threads account via Meta Graph API.",
        epilog="Reference: https://developers.facebook.com/documentation/threads/posts.md",
    )
    ap.add_argument("text", nargs="?", default="",
                    help="Post text (max 500 chars). Optional for image-only and threaded posts.")
    ap.add_argument("--image", help="Path to a local image (jpg/png/webp). Auto-hosted.")
    ap.add_argument("--image-url", help="Public image URL to use directly (skips hosting).")
    ap.add_argument("--images", nargs="+", metavar="PATH",
                    help="2+ local images -> post as a Threads carousel (auto-hosted).")
    ap.add_argument("--image-urls", nargs="+", metavar="URL",
                    help="2+ public image URLs -> post as a Threads carousel.")
    ap.add_argument("--reply-to", help="Post ID to reply to.")
    ap.add_argument("--thread", metavar="REPLY_TEXT",
                    help="Post image, then text as a reply (2-post thread). "
                         "REPLY_TEXT becomes the second post.")
    ap.add_argument("--thread-text-first", metavar="LEAD_TEXT",
                    help="Post text, then image as a reply (2-post thread). "
                         "LEAD_TEXT becomes the first post; image is the second.")
    ap.add_argument("--text-thread", metavar="POSTS",
                    help="Post a chain of text containers, each replying to the previous. "
                         "Accepts @/path/to/file.json (JSON array of strings), "
                         "a literal JSON array, or '|||'-delimited text. "
                         "Max 10 posts, each <=500 chars. Bails on first failure.")
    ap.add_argument("--dry-run", action="store_true", help="Print payloads, don't POST.")
    ap.add_argument("--visibility", default="PUBLIC", choices=["PUBLIC", "FOLLOWERS"],
                    help="Text post visibility (Threads API currently treats as PUBLIC regardless; default PUBLIC).")
    args = ap.parse_args()

    carousel_flags = [args.images, args.image_urls]
    if args.images and args.image_urls:
        ap.error("--images and --image-urls are mutually exclusive.")
    if (args.images or args.image_urls) and (args.image or args.image_url):
        ap.error("Use --images/--image-urls for carousels, not --image/--image-url.")
    if (args.images or args.image_urls) and any(x is not None for x in (args.thread, args.thread_text_first, args.text_thread)):
        ap.error("Carousel cannot be combined with --thread/--thread-text-first/--text-thread.")

    thread_flags = [args.thread, args.thread_text_first, args.text_thread]
    if sum(x is not None for x in thread_flags) > 1:
        ap.error("--thread, --thread-text-first, and --text-thread are mutually exclusive.")

    if args.text_thread is not None:
        texts = _load_text_thread_arg(args.text_thread)
        result = post_text_thread(texts=texts, dry_run=args.dry_run)
    elif args.thread:
        result = post_image_then_text(
            image_path=args.image, image_url=args.image_url,
            reply_text=args.thread, dry_run=args.dry_run,
        )
    elif args.thread_text_first:
        result = post_text_then_image(
            lead_text=args.thread_text_first,
            image_path=args.image, image_url=args.image_url, dry_run=args.dry_run,
        )
    elif args.images or args.image_urls:
        result = post_carousel(
            text=args.text, image_paths=args.images, image_urls=args.image_urls,
            reply_to=args.reply_to, dry_run=args.dry_run,
        )
    elif args.image or args.image_url:
        result = post_image(
            text=args.text, image_path=args.image, image_url=args.image_url,
            reply_to=args.reply_to, dry_run=args.dry_run,
        )
    else:
        result = post_text(text=args.text, reply_to=args.reply_to, dry_run=args.dry_run)

    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except ThreadsPostError as exc:
        sys.stderr.write(f"error: {exc}\n")
        sys.exit(1)
