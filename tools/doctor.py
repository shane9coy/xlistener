#!/usr/bin/env python3
"""doctor.py — one-shot health and configuration check for XListener.

Prints a PASS/WARN/FAIL line for every precondition the listener depends on, with
the exact next action next to each failure. Designed to be run by an agent: exit
code 0 = ready, 1 = something needs fixing, and `--json` gives machine-readable
output.

It never prints secret values, never reads the X timeline, and never publishes
anything. The two X calls it makes are management reads (/2/webhooks and
/2/activity/subscriptions) which return zero posts and therefore cost nothing.

Usage:
    python tools/doctor.py                 # human-readable
    python tools/doctor.py --json          # machine-readable
    python tools/doctor.py --offline       # skip anything that touches the network
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import platform
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import mirror as m  # noqa: E402

try:
    import requests
except ImportError:  # pragma: no cover
    print("FAIL  requests not installed — run: pip install -r requirements.txt")
    sys.exit(1)

RESULTS: list[dict] = []


def record(level: str, name: str, detail: str = "", action: str = "") -> None:
    RESULTS.append({"level": level, "check": name, "detail": detail, "action": action})
    if not QUIET:
        icon = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL", "SKIP": "SKIP"}[level]
        line = f"{icon}  {name}"
        if detail:
            line += f" — {detail}"
        print(line)
        if action and level in ("WARN", "FAIL"):
            print(f"      → {action}")


def ok(name, detail="", action=""):
    record("PASS", name, detail, action)


def warn(name, detail="", action=""):
    record("WARN", name, detail, action)


def fail(name, detail="", action=""):
    record("FAIL", name, detail, action)


def skip(name, detail=""):
    record("SKIP", name, detail)


def load_env(cfg: dict | None = None) -> dict:
    """Read <env_dir>/.env, where env_dir is the configured (repo-relative) path."""
    env_dir = m.resolve_dir((cfg or {}).get("env_dir"), REPO)
    p = env_dir / ".env"
    if not p.exists():
        return {}
    return m.load_env_file(p)


def main() -> int:
    global QUIET
    ap = argparse.ArgumentParser(description="Check XListener configuration and health.")
    ap.add_argument("--json", action="store_true", help="machine-readable output (implies quiet)")
    ap.add_argument("--offline", action="store_true", help="skip network checks")
    ap.add_argument("--config", default=str(REPO / "config.json"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    QUIET = args.quiet or args.json

    # ---------- 1. runtime ----------
    if sys.version_info >= (3, 10):
        ok("python >= 3.10", platform.python_version())
    else:
        fail("python >= 3.10", platform.python_version(), "install Python 3.10+ and recreate the venv")

    missing = [mod for mod in ("fastapi", "uvicorn", "requests")
               if not __import__("importlib").util.find_spec(mod)]
    if missing:
        fail("python dependencies", f"missing: {', '.join(missing)}", "pip install -r requirements.txt")
    else:
        ok("python dependencies", "fastapi, uvicorn, requests")

    # ---------- 2. config + env ----------
    cfg_path = Path(args.config)
    cfg: dict = {}
    if not cfg_path.exists():
        fail("config.json exists", cfg_path.name, "cp config.example.json config.json and fill it in")
    else:
        try:
            cfg = m.load_config(cfg_path)
            ok("config.json parses", str(cfg_path.name))
        except json.JSONDecodeError as e:
            fail("config.json parses", str(e), "fix the JSON syntax")
            cfg = {}
    if cfg:
        if cfg.get("x_user_id"):
            ok("config.x_user_id", str(cfg["x_user_id"]))
        else:
            fail("config.x_user_id", "missing — required, no auto-resolve",
                 "set x_user_id in config.json (get it: ./venv/bin/python tools/resolve_user_id.py)")

    env = load_env(cfg)
    if not env:
        env_path = m.resolve_dir(cfg.get("env_dir"), REPO) / ".env"
        fail(".env exists", f"not found at {env_path}",
             "cp .env.example .env and fill in the credentials")
    else:
        ok(".env exists", f"{len(env)} keys")
        required = ["X_CONSUMER_KEY", "X_CONSUMER_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET",
                    "X_BEARER_TOKEN", "THREADS_ACCESS_TOKEN", "THREADS_USER_ID"]
        absent = [k for k in required if not (env.get(k) or "").strip()]
        if absent:
            fail(".env required keys", f"missing: {', '.join(absent)}", "fill them in .env (see .env.example)")
        else:
            ok(".env required keys", f"{len(required)} required keys present")
        if (env.get("X_OAUTH2_CLIENT_SECRET") or "").strip():
            ok("OAuth 2.0 client secret", "present — CRC and OAuth2 signatures enabled")
        else:
            warn("OAuth 2.0 client secret", "absent — legacy OAuth1 secret only",
                 "add X_OAUTH2_CLIENT_SECRET if X reports webhook valid:true but no events arrive")

    # Only meaningful inside a checkout: a deployed install is not a git repo.
    if not (REPO / ".git").exists():
        skip(".gitignore covers secrets", "not a git checkout (deployed install)")
    else:
        gitignore = (REPO / ".gitignore").read_text(errors="replace") if (REPO / ".gitignore").exists() else ""
        if ".env" in gitignore and "state.db" in gitignore:
            ok(".gitignore covers secrets", ".env, state.db")
        else:
            fail(".gitignore covers secrets", "missing .env or state.db", "restore the shipped .gitignore before committing")

    # ---------- 3. local service ----------
    host = ((cfg.get("webhook") or {}).get("host") or "127.0.0.1").strip()
    port = int((cfg.get("webhook") or {}).get("port") or 8644)
    if args.offline:
        skip("listener /health", "offline mode")
    else:
        try:
            r = requests.get(f"http://{host}:{port}/health", timeout=3)
            if r.status_code == 200:
                ok("listener /health", f"http://{host}:{port}/health")
            else:
                fail("listener /health", f"HTTP {r.status_code}", "check: systemctl --user status xlistener")
        except requests.RequestException:
            fail("listener /health", "unreachable", "start it: systemctl --user start xlistener")

    db = REPO / (cfg.get("state_db_path") or "state.db")
    if db.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            n = conn.execute("SELECT count(*) FROM mirrored").fetchone()[0]
            errors = conn.execute("SELECT count(*) FROM mirrored WHERE error IS NOT NULL").fetchone()[0]
            ok("state.db readable", f"{n} rows, {errors} with errors")
            conn.close()
        except Exception as e:  # noqa: BLE001
            fail("state.db readable", str(e), "delete a corrupt state.db only if you accept re-mirroring old posts")
    else:
        warn("state.db missing", "created on first start")

    try:
        (REPO / (cfg.get("media_cache_dir") or "media_cache")).mkdir(parents=True, exist_ok=True)
        probe = REPO / (cfg.get("media_cache_dir") or "media_cache") / ".write-probe"
        probe.write_text("ok")
        probe.unlink()
        ok("media_cache writable")
    except OSError as e:
        fail("media_cache writable", str(e), "fix permissions on the repo directory")

    # ---------- 4. budget ----------
    try:
        import x_budget
        b = x_budget.get_budget(REPO, limits=cfg.get("x_budget"))
        ok("X spend budget", b.summary())
    except Exception as e:  # noqa: BLE001
        warn("X spend budget", str(e))

    if args.offline:
        skip("X webhook + subscriptions", "offline mode")
        skip("Threads credentials", "offline mode")
        skip("public CRC self-test", "offline mode")
        return finish(args)

    # ---------- 5. X side (management reads only: zero posts, zero cost) ----------
    bearer = (env.get("X_BEARER_TOKEN") or "").strip()
    webhook_url = None
    if not bearer:
        fail("X webhook", "no bearer token", "set X_BEARER_TOKEN in .env")
    else:
        try:
            r = requests.get("https://api.x.com/2/webhooks",
                             headers={"Authorization": f"Bearer {bearer}"}, timeout=20)
            if r.status_code != 200:
                fail("X webhook list", f"HTTP {r.status_code}: {r.text[:120]}",
                     "check the bearer token's app has webhook access")
            else:
                hooks = r.json().get("data") or []
                if not hooks:
                    fail("X webhook registered", "none",
                         "register one: see AGENTS.md phase 4 (POST /2/webhooks)")
                else:
                    h = hooks[0]
                    webhook_url = h.get("url")
                    lvl = ok if h.get("valid") else fail
                    lvl("X webhook registered", f"id={h.get('id')} valid={h.get('valid')}",
                        "re-validate: PUT /2/webhooks/{id} (forces a CRC to your URL)")
        except requests.RequestException as e:
            fail("X webhook list", str(e), "network problem reaching api.x.com")

    if bearer:
        try:
            r = requests.get("https://api.x.com/2/activity/subscriptions",
                             headers={"Authorization": f"Bearer {bearer}"}, timeout=20)
            if r.status_code != 200:
                warn("X Activity subscriptions", f"HTTP {r.status_code}", "may need a tier that includes XAA")
            else:
                data = r.json().get("data") or []
                if isinstance(data, dict):
                    data = [data]
                types = sorted({s.get("event_type", "?") for s in data})
                if "post.create" in types:
                    ok("X Activity subscription", f"event_types={types}")
                else:
                    fail("X Activity subscription", f"event_types={types or 'none'}",
                         "create one: python tools/setup_xaa.py --url <your webhook url> --apply")
        except requests.RequestException as e:
            fail("X Activity subscriptions", str(e), "network problem reaching api.x.com")

    # ---------- 6. Threads side ----------
    token = (env.get("THREADS_ACCESS_TOKEN") or "").strip()
    uid = (env.get("THREADS_USER_ID") or "").strip()
    if not token or not uid:
        fail("Threads credentials", "missing token or user id", "see .env.example")
    else:
        try:
            r = requests.get(f"https://graph.threads.net/v1.0/{uid}",
                             params={"fields": "id,username", "access_token": token}, timeout=20)
            body = r.json() if r.content else {}
            if r.status_code == 200 and body.get("id"):
                ok("Threads credentials", f"@{body.get('username', '?')} (id {body.get('id')})")
            else:
                fail("Threads credentials", f"HTTP {r.status_code}: {str(body)[:120]}",
                     "token may be expired — re-auth with tools/threads_oauth.py")
        except requests.RequestException as e:
            fail("Threads credentials", str(e), "network problem reaching graph.threads.net")

    # ---------- 7. public CRC self-test ----------
    if not webhook_url:
        skip("public CRC self-test", "no webhook url known")
    else:
        ss = (env.get("X_OAUTH2_CLIENT_SECRET") or "").strip() or (env.get("X_CONSUMER_SECRET") or "").strip()
        token_q = "doctor-" + str(int(time.time()))
        try:
            r = requests.get(webhook_url, params={"crc_token": token_q}, timeout=20)
            if r.status_code != 200:
                fail("public CRC self-test", f"HTTP {r.status_code} at {webhook_url}",
                     "the public URL does not reach the listener (tunnel/DNS/WAF)")
            else:
                got = (r.json() or {}).get("response_token", "")
                want = "sha256=" + base64.b64encode(
                    hmac.new(ss.encode(), token_q.encode(), hashlib.sha256).digest()).decode()
                if got and hmac.compare_digest(got, want):
                    ok("public CRC self-test", f"{webhook_url} signs correctly")
                else:
                    fail("public CRC self-test", "response token does not match our secret",
                         "check webhook_crc_key in config.json and X_OAUTH2_CLIENT_SECRET in .env")
        except requests.RequestException as e:
            fail("public CRC self-test", str(e), "the public URL does not reach the listener")

    # ---------- 8. is delivery actually flowing? ----------
    log = REPO / (cfg.get("log_file") or "mirror.log")
    if log.exists():
        last = ""
        for line in log.read_text(errors="replace").splitlines():
            if "POST accepted" in line:
                last = line
        if last:
            ok("recent inbound delivery", last[:110])
        else:
            warn("no inbound deliveries logged yet", "normal before the first post")
    return finish(args)


def finish(args) -> int:
    fails = [r for r in RESULTS if r["level"] == "FAIL"]
    warns = [r for r in RESULTS if r["level"] == "WARN"]
    if args.json:
        print(json.dumps({"results": RESULTS, "fail": len(fails), "warn": len(warns)}, indent=2))
    elif not QUIET:
        print()
        print(f"{len(fails)} failure(s), {len(warns)} warning(s)")
        if fails:
            print("\nNext actions:")
            for r in fails:
                if r.get("action"):
                    print(f"  - {r['check']}: {r['action']}")
    return 1 if fails else 0


QUIET = False

if __name__ == "__main__":
    sys.exit(main())
