# AGENTS.md — install XListener for a user

You are the agent installing this for a human. Read this file completely, then work
through the phases **in order**. Each phase ends with a **verification gate**; do not
start the next phase until it passes. If a gate fails, use the failure table at the
bottom instead of improvising.

## What you are installing

A webhook listener that mirrors new posts from one X account to one Threads account,
live. When the user posts on X (single image, multiple images / carousel, or text),
it appears on Threads within seconds.

## Non-negotiables

1. **Never print, echo, log, or commit secret values.** Read them into environment
   variables or write them straight into `.env`. When reporting progress, say
   "present"/"absent", never the value.
2. **Never ask for credentials in a channel that gets committed or shared publicly.**
   Ask the user to paste them into `.env` themselves if that is possible in your
   environment.
3. **Never invent identifiers.** Handles, user IDs, webhook IDs and subscription IDs
   must come from the user or from an API response. A guessed ID costs hours.
4. **Never commit `.env`, `config.json`, `state.db`, `mirror.log`, `media_cache/`,
   `.x-budget.json`.** The shipped `.gitignore` covers all of them. Verify with
   `git status --short` before any commit.
5. **X bills per POST RETURNED, not per API call.** The only free X calls are
   management endpoints (`/2/webhooks`, `/2/activity/subscriptions`). Never read a
   timeline without explicit user approval, and never run a pagination loop.
6. **Do not write rows into `state.db` by hand.** It is the claim ledger: a fake
   `ok` row permanently suppresses the real post that has that id, and a fake
   `pending` row blocks it for 15 minutes. Use `tools/replay_backlog.py --reset-errors`
   to clear genuine failures instead.

## Phase 0 — collect inputs (ask, don't guess)

Ask the user for these, all at once, in the format shown. Tell them not to paste
secrets into a public channel.

| # | Value | Where the user gets it | Format |
|---|---|---|---|
| 1 | X handle to mirror | the account | `@example_user` |
| 2 | Threads handle to publish to | the account | `@example_threads_user` |
| 3 | X OAuth 1.0a consumer key + secret | X Developer Console → app → Keys and tokens | opaque strings |
| 4 | X access token + secret (same app, user context) | same page, "Access token and secret" | opaque strings |
| 5 | X app-only **Bearer Token** | same page | starts with `A…` |
| 6 | X **OAuth 2.0 Client Secret** | same page, "OAuth 2.0 Client ID and Client Secret" | opaque string |
| 7 | Public HTTPS hostname you control | e.g. a domain on Cloudflare | `listen.example.com` |
| 8 | Threads app id + secret | Meta app dashboard → Threads API use case | numeric + secret |
| 9 | Threads long-lived access token + numeric user id | Phase 2 produces these | token + digits |

If the user does not have (3)–(6), walk them through Phase 3 before continuing. If
they do not have a domain, Phase 4 covers a tunnel.

**Gate 0:** you can list all nine values (or a written plan to obtain them).

## Phase 1 — install

```bash
git clone <repo> xlistener && cd xlistener
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp config.example.json config.json
cp .env.example .env && chmod 600 .env
```

Write the secret values from Phase 0 into `.env` (keys are named in the file's
comments). Fill `config.json`:

- `x_user_handle` — without `@`
- `threads_handle` — without `@`
- `x_user_id` — **required** numeric id of the account to mirror. The listener does
  not auto-resolve it (that costs a billed call on every boot and an empty value
  makes every delivery fail the author check). If you do not know it:
  `./venv/bin/python tools/resolve_user_id.py --handle <handle>` — one *user* lookup,
  not a post read — then paste the id into `config.json`
- everything else can stay at its default

**Gate 1:** `./venv/bin/python tools/doctor.py --offline` runs and shows the runtime
and file checks passing. Expect exactly **two** FAILs at this point — `.env required
keys` (the Threads values arrive in Phase 2) and `config.x_user_id` (Phase 1 supplies
it). Everything else that runs offline should PASS; `state.db missing` and the
listener/Threads/CRC checks will be WARN or SKIP until later phases. If `.env exists`, `config.json parses`,
`.gitignore covers secrets` or `python dependencies` fail, fix those first.

## Phase 2 — Threads credentials

The user must authorize a Meta app once, in a browser. This cannot be fully
automated; you are guiding them.

1. Meta app dashboard → use case **"Access the Threads API"** → Settings → **Threads
   App ID** and **Threads App Secret**. Note: the *Facebook* app id is a different
   number and will be rejected by `graph.threads.net` — always use the Threads id.
2. Add redirect URI **`http://localhost:8899/callback`** to the Meta app. That is
   what `tools/threads_oauth.py` listens on by default; the URI registered in the
   dashboard must match it character-for-character or the token exchange fails with
   a redirect_uri mismatch. (Use `--port N` and register `http://localhost:N/callback`
   if 8899 is taken.)
3. Run `./venv/bin/python tools/threads_oauth.py` — it prints an authorization URL,
   starts the localhost server, captures the callback code, exchanges it for a
   **long-lived (60-day)** token, verifies it, and writes `THREADS_APP_ID`,
   `THREADS_APP_SECRET`, `THREADS_ACCESS_TOKEN` and `THREADS_USER_ID` into `.env`.

**Gate 2:** `python tools/doctor.py` shows `PASS  Threads credentials — @<handle>`.
The token expires in 60 days; set up a reminder/cron (`tools/refresh_threads_token.py`)
inside Meta's refresh window. A dead token = the listener logs `[ERR post]` for every
event, which looks like a delivery outage.

## Phase 3 — X app credentials (skip if Phase 0 supplied them)

X Developer Console → Projects & Apps → the app → **Keys and tokens**:

- *Consumer Keys* → API Key / API Key Secret → `X_CONSUMER_KEY` / `X_CONSUMER_SECRET`
- *Authentication Tokens* → Access Token / Secret → `X_ACCESS_TOKEN` / `X_ACCESS_TOKEN_SECRET`
- *Authentication Tokens* → **Bearer Token** → `X_BEARER_TOKEN`
- *OAuth 2.0 Client ID and Client Secret* → Client Secret → `X_OAUTH2_CLIENT_SECRET`

The app must be in a Project and have access to the Account Activity / Activity API
product. The bearer token, not the OAuth1 keys, is what registers webhooks and
creates subscriptions.

**Gate 3:** `./venv/bin/python tools/doctor.py --offline` shows
`PASS  .env required keys` (Phase 2 supplied the Threads half) and
`PASS  OAuth 2.0 client secret` (or a WARN if the app is OAuth1-only).

## Phase 4 — public HTTPS endpoint + webhook registration

X requires an HTTPS URL with **no port** that answers a CRC GET. Pick one:

**Option A — Cloudflare Tunnel** (no inbound ports, works behind NAT):

```bash
cloudflared tunnel create xlistener
cloudflared tunnel route dns xlistener listen.example.com
# copy deploy/cloudflared.example.yml → ~/.cloudflared/config.yml, set tunnel id,
# point listen.example.com at http://127.0.0.1:8644
sudo systemctl enable --now cloudflared
```

**Option B — any reverse proxy** (Caddy/nginx) terminating TLS and proxying
`listen.example.com` → `127.0.0.1:8644`.

Then start the listener and register the webhook:

```bash
# foreground first, to watch it boot
./venv/bin/python webhook_listener.py
# in another shell, register (this triggers a CRC immediately):
curl -s -X POST https://api.x.com/2/webhooks \
  -H "Authorization: Bearer $X_BEARER_TOKEN" -H "Content-Type: application/json" \
  -d '{"url":"https://listen.example.com/webhooks/twitter"}'
```

Expected: `{"data":{"id":"…","url":"…","valid":true,…}}`.

**Gate 4:** `python tools/doctor.py` shows `PASS  X webhook registered … valid=True`
and `PASS  public CRC self-test`.

> **If the CRC fails:** the URL did not answer correctly. Check the tunnel, that the
> URL has no port, that the listener is running, and that `webhook_crc_key` in
> `config.json` matches which secret X expects (`oauth2` when `X_OAUTH2_CLIENT_SECRET`
> is set, `legacy` for OAuth1-only apps). `CrcValidationFailed` on the POST means X
> could not reach you at all.

## Phase 5 — create the event subscription (this is the step everyone misses)

A registered webhook delivers **nothing** until an event subscription points at it.
In the Account Activity API era one all-or-nothing subscription covered everything;
in the X Activity API (XAA) each event type is its own subscription. For post
mirroring you need exactly one:

```bash
./venv/bin/python tools/setup_xaa.py --url https://listen.example.com/webhooks/twitter          # dry run
./venv/bin/python tools/setup_xaa.py --url https://listen.example.com/webhooks/twitter --apply  # create
```

Expected: `POST /2/activity/subscriptions post.create -> HTTP 200`.

**Gate 5:** `python tools/doctor.py` shows
`PASS  X Activity subscription — event_types=['post.create']`.

> Account Activity (AAA) reaches end of life **2027-01-31**. XAA is its replacement.
> If the user still has an old AAA subscription, X delivers each post **twice**
> (once per format) and keeps sending `favorite_events`. That is harmless — the claim
> ledger suppresses the duplicate — but remove the AAA subscription once XAA delivery
> is confirmed:
>
> ```bash
> curl -s -X DELETE \
>   -H "Authorization: Bearer $X_BEARER_TOKEN" \
>   "https://api.x.com/2/account_activity/webhooks/<webhook_id>/subscriptions/<x_user_id>/all"
> # expect {"data":{"subscribed":false}}; then verify:
> curl -s -H "Authorization: Bearer $X_BEARER_TOKEN" \
>   "https://api.x.com/2/account_activity/webhooks/<webhook_id>/subscriptions/all/list"
> # expect "subscriptions": []
> ```

## Phase 6 — run it as a service and verify end to end

```bash
mkdir -p ~/.config/systemd/user
cp deploy/xlistener.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now xlistener
loginctl enable-linger "$USER"     # keep it running after logout
```

Verification, cheapest first:

0. **Observe mode first.** Set `"observe_only": true` under `webhook` in
   `config.json`, restart, and let it see a post or two. It logs
   `[OBSERVE] would post tweet …` and publishes nothing, uploads no media to any
   image host, and writes no `state.db` rows. This proves delivery, signature verification, author filtering
   and media download before anything reaches the user's account. Then set it back
   to `false` and restart.
1. **Auth path, no side effects** — sign an empty payload with the consumer secret
   and POST it to the public URL. A `200 {"processed": []}` proves routing + HMAC.
   An unsigned POST must return `401`.
2. **`python tools/doctor.py`** — every line PASS (WARNs are acceptable).
3. **Real post** — ask the user to publish one X post (ideally with 2–3 images so the
   carousel path is exercised), then watch:

```
tail -f mirror.log
```

| Expected log line | Meaning |
|---|---|
| `POST accepted … event types=['data']` | XAA delivery |
| `XAA post.create event_uuid=… post_id=…` | payload parsed |
| `mirroring tweet … with 3 photo(s)` | media collected |
| `[OK] mirrored tweet … → threads https://www.threads.com/…` | **done** |

**Gate 6:** one real post appears on Threads, including all images.

## Phase 7 — handover

Tell the user: which handles are linked, that X charges $0.005 per delivered
`post.create` event and per post read by any tool, what the budget caps are
(`python x_budget.py` shows today's spend), how to watch logs (`tail -f mirror.log`),
that a `200` from the webhook means queued (not published — wait for `[OK]`), and that
the Threads token needs refreshing before day 60.

## Failure table

Match the log line or API error, apply the action, do not guess.

| Symptom in `mirror.log` / API | Cause | Action |
|---|---|---|
| `POST rejected: invalid or missing signature (… signature headers present=[X-Twitter-Webhooks-Signature-OAuth2])` | X signs with the OAuth 2.0 client secret but `.env` has none/old one | Set `X_OAUTH2_CLIENT_SECRET`; restart |
| `POST rejected … signature headers present=none` | Not from X (or a proxy is stripping headers) | Ensure the proxy forwards headers untouched |
| `event types=['data']` but then `ignoring XAA event_type=…` | Subscription is for another event type | Only `post.create` is mirrored; check the subscription |
| `event counts: {'tweet_create_events': 0}` | X delivered an **empty** event array (seen in the wild) | Nothing to mirror; if persistent, this is an X-side fault |
| `[SKIP not-author] tweet … author=…` | `x_user_id` in `config.json` is wrong | Fix to the account's numeric id (`tools/resolve_user_id.py`) |
| `413 payload too large` in the access log | Body exceeded the 1 MiB cap | X payloads are far smaller; check for a proxy/scanning layer sending junk |
| Listener exits with code 2 and "missing a numeric x_user_id" | `config.json` has no `x_user_id` | Run `tools/resolve_user_id.py`, then restart |
| `200 {"queued": true}` but no `[OK]` line | Publishing is asynchronous; the background task failed and recorded an `error` row | Read the `[ERR …]` line above it; the next delivery retries automatically |
| `[SKIP reply]` / `[SKIP retweet]` / `[SKIP quote]` | Working as designed | Quotes: set `webhook.mirror_quote_posts` |
| `[SKIP video-only]` | Video is not mirrored | Out of scope |
| `[ERR post] tweet …: threads-post failed` | Threads publish failed | Read the stderr in the log; check token (Phase 2) |
| `HTTP 402` / `credits depleted` on any X call | X credit balance is empty. **Webhook delivery also stops silently** while it is | Top up in the console; verify with `GET /2/webhooks` + a test post |
| `valid: true` but no events ever arrive | CRC only re-runs after 24h, so `valid` can be stale; or no subscription exists | Create the subscription (Phase 5); force a CRC with `PUT /2/webhooks/{id}` |
| `Meta said success but the post is not live` | Threads sometimes returns an id for an invalid post | `threads_poster.py` already re-creates and retries; check the permalink |
| `Image Not Found` / `OAuthException code 1` on image posts | Meta could not fetch `image_url` | Upstream image host flaked; the poster HEAD-verifies URLs — retry |
| Carousel fails with a child not ready | Children must be `FINISHED` before the parent is created | Already handled; if manual, poll `GET /{container-id}?fields=status` |

## Do not do

- Do not run `tools/replay_backlog.py` without a user-approved `--since`: it reads
  the X timeline and is billed per post returned.
- Do not delete `state.db` to "fix" anything — it is the claim ledger; deleting it
  re-mirrors history. Failed rows are retryable automatically; clear them with
  `tools/replay_backlog.py --reset-errors` if you need a manual reset.
- Do not raise the `x_budget` caps to get past a failure. A spend cap firing means
  something is looping; find the loop.
- Do not POST samples with real tweet ids into `state.db`.
