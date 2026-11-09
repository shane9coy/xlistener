#!/usr/bin/env python3
"""threads_oauth.py — one-time Threads authorization, producing a 60-day token.

Meta requires a human to approve the app in a browser, so this script starts a tiny
localhost callback server, prints the authorization URL, waits for the redirect, then
exchanges the code for a long-lived token and writes it into .env.

Prerequisites (Meta app dashboard → use case "Access the Threads API"):
  - Threads App ID and Threads App Secret
  - Redirect URI http://localhost:8899/callback added to the app
    (change with --port; the URI registered in the dashboard must match exactly)

Usage:
    ./venv/bin/python tools/threads_oauth.py
    ./venv/bin/python tools/threads_oauth.py --port 8899
    ./venv/bin/python tools/threads_oauth.py --app-id <id> --app-secret <secret>

Writes: THREADS_ACCESS_TOKEN, THREADS_USER_ID, THREADS_APP_ID, THREADS_APP_SECRET
This script never prints the token. Re-running it re-authorizes (deleting nothing).
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENV_FILE = REPO / ".env"
SCOPES = "threads_basic,threads_content_publish"


def load_env(path: Path) -> dict:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def save_env(path: Path, updates: dict[str, str]) -> None:
    text = path.read_text() if path.exists() else ""
    out, seen = [], set()
    for line in text.splitlines():
        m = re.match(r"^([A-Z_][A-Z0-9_]*)\s*=", line)
        if m and m.group(1) in updates:
            out.append(f"{m.group(1)}={updates[m.group(1)]}")
            seen.add(m.group(1))
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    path.write_text("\n".join(out).rstrip() + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser(description="Authorize the Threads app and store a long-lived token.")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--app-id", help="Threads App ID (default: THREADS_APP_ID from .env)")
    ap.add_argument("--app-secret", help="Threads App Secret (default: THREADS_APP_SECRET from .env)")
    args = ap.parse_args()

    env = load_env(ENV_FILE)
    app_id = (args.app_id or env.get("THREADS_APP_ID") or "").strip()
    app_secret = (args.app_secret or env.get("THREADS_APP_SECRET") or "").strip()
    if not app_id or not app_secret:
        print("Missing Threads App ID / Secret.\n"
              "  - Copy the THREADS_APP_ID / THREADS_APP_SECRET lines into .env, or\n"
              "  - pass --app-id and --app-secret.\n"
              "Use the *Threads* App ID from the 'Access the Threads API' use case,\n"
              "not the Facebook App ID — graph.threads.net rejects the Facebook one.",
              file=sys.stderr)
        return 1

    redirect_uri = f"http://localhost:{args.port}/callback"
    state = secrets.token_urlsafe(16)
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            if qs.get("error"):
                result["error"] = qs["error"][0]
            elif qs.get("state", [""])[0] != state:
                result["error"] = "state mismatch (possible CSRF); re-run the script"
            elif qs.get("code"):
                result["code"] = qs["code"][0]
            body = b"<h2>Authorization received.</h2><p>You can close this tab and return to the terminal.</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, *a):  # silence request logging
            return

    server = http.server.HTTPServer(("127.0.0.1", args.port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    auth_url = (
        "https://threads.net/oauth/authorize?"
        + urllib.parse.urlencode({
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "response_type": "code",
            "state": state,
        })
    )
    print("1) Open this URL and approve the app:\n")
    print("   " + auth_url + "\n")
    print(f"2) Waiting for the redirect on {redirect_uri} …")
    try:
        webbrowser.open(auth_url)
    except Exception:  # noqa: BLE001
        pass

    if not done.wait(timeout=300):
        print("\nTimed out after 5 minutes. Re-run and approve faster.", file=sys.stderr)
        return 1
    server.shutdown()

    if result.get("error"):
        print(f"Authorization failed: {result['error']}\n"
              f"Most common cause: {redirect_uri} is not registered on the Meta app.", file=sys.stderr)
        return 1

    print("3) Exchanging the code for a short-lived token …")
    short = post_form("https://graph.threads.net/oauth/access_token", {
        "client_id": app_id,
        "client_secret": app_secret,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "code": result["code"],
    })
    short_token = short.get("access_token") or ""
    user_id = str(short.get("user_id") or "")
    if not short_token or not user_id:
        print(f"No token in response: {json.dumps({k: v for k, v in short.items() if k != 'access_token'})}",
              file=sys.stderr)
        return 1

    print("4) Upgrading to a 60-day long-lived token …")
    try:
        long_resp = get_json(
            "https://graph.threads.net/access_token?"
            + urllib.parse.urlencode({
                "grant_type": "th_exchange_token",
                "client_secret": app_secret,
                "access_token": short_token,
            })
        )
        long_token = long_resp.get("access_token") or short_token
        expires_in = long_resp.get("expires_in")
    except Exception as e:  # noqa: BLE001
        print(f"  long-lived exchange failed ({e}); keeping the short-lived token", file=sys.stderr)
        long_token, expires_in = short_token, None

    print("5) Verifying the token against the Threads graph …")
    try:
        who = get_json(f"https://graph.threads.net/v1.0/{user_id}"
                       f"?fields=id,username&access_token={urllib.parse.quote(long_token)}")
    except Exception as e:  # noqa: BLE001
        print(f"  verification failed: {e}", file=sys.stderr)
        who = {}

    save_env(ENV_FILE, {
        "THREADS_APP_ID": app_id,
        "THREADS_APP_SECRET": app_secret,
        "THREADS_ACCESS_TOKEN": long_token,
        "THREADS_USER_ID": user_id,
    })
    print(f"\nWrote credentials to {ENV_FILE} (chmod 600).")
    if who.get("username"):
        print(f"Authorized Threads account: @{who['username']} (id {who.get('id')})")
    if expires_in:
        print(f"Token lifetime: ~{int(expires_in) // 86400} days. Refresh before expiry with "
              "tools/refresh_threads_token.py.")
    print("\nNext: python tools/doctor.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
