![tests](https://github.com/<owner>/xlistener/actions/workflows/tests.yml/badge.svg)

# XListener

Mirror new posts from one X account to one Threads account in real time — text, single images, and multi-image carousels.

## Why this exists

X is replacing the Account Activity API with the X Activity API (AAA EOL 2027-01-31). Most snippets parse only the legacy payload and verify only the legacy signature header, going silent when X switches schemes. XListener handles both, plus carousels and a spend cap.

## Features

- **Dual payload support** — AAA `tweet_create_events` and the XAA `data.event_type` envelope share one filter and mirror path.
- **Dual signature headers** — `X-Twitter-Webhooks-Signature-OAuth2` (preferred, OAuth 2.0 client secret) with the legacy `X-Twitter-Webhooks-Signature` as fallback.
- **CRC endpoint with a signing-oracle guard** — only opaque tokens are signed, so it cannot forge an event.
- **Carousel publishing** — children → parent → publish, the only order Threads accepts.
- **Idempotent dedupe** — SQLite `state.db` stops a retried or duplicated delivery from double-posting.
- **Hard spend caps** — per-call and per-day caps enforced before any X timeline read, persisted in `.x-budget.json`.
- **Webhook-only live path** — no polling loop; `mirror.py` is kept for manual backfills only.

## Quick start

If you use an AI coding agent, paste:

> *Read AGENTS.md and install XListener for me. Ask me for the credentials it lists in Phase 0.*

Five commands:

```bash
git clone <repo> xlistener && cd xlistener
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp config.example.json config.json
cp .env.example .env && chmod 600 .env
./venv/bin/python tools/doctor.py
```

Then fill `.env` and `config.json` and follow the phase gates in [AGENTS.md](AGENTS.md); it covers credentials, webhook registration, the `post.create` subscription, and the service.

### Run it in observe mode first

Set `"observe_only": true` under `webhook` in `config.json` for the first day. The listener then runs its full path — receives, verifies, filters, downloads media — but publishes nothing and touches no state:

```
[OBSERVE] would post tweet 1234567890123456789: text='gm' image=yes
```

Watch `mirror.log`, confirm the posts it *would* mirror are the ones you expect, then set it back to `false` and restart. That catches a wrong `x_user_id` or an unexpected filter decision before anything reaches your account.

## Architecture

```
X post (AAA or XAA event)
  │
  ▼
X webhook ── POST /webhooks/twitter, HMAC-signed
  │
  ▼
public HTTPS (no port)
  │  tunnel / reverse proxy
  ▼
listener :8644
  │  filters: author / original / reply / quote / video
  ▼
download media from pbs.twimg.com (https only)
  │
  ▼
threads_poster.py
  │
  ▼
Threads Graph API

CRC GET /webhooks/twitter ──► answered separately
```

## What gets mirrored

| X post | Result |
|---|---|
| Original text post | Threads text post |
| Original post, 1 image | Threads image post |
| Original post, 2–4 images (X's maximum) | Threads carousel, caption on the parent |
| Reply | Skipped |
| Repost | Skipped |
| Quote post | Skipped by default; `webhook.mirror_quote_posts` opts in |
| Video-only post | Skipped |

## Cost

X bills per post, not per call. Management endpoints (`GET /2/webhooks`, `GET /2/activity/subscriptions`) cost nothing; anything returning posts is billed.

| Item | Rate | Default cap |
|---|---|---|
| Delivered `post.create` event | $0.005 per event | none — one per new post |
| Timeline read | $0.005 per post returned | 10 posts/call, 10 calls/day, 10 posts/day |
| Worst case at default caps | — | $0.05/day |

`python x_budget.py` prints today's spend. `X_MAX_*` can lower the caps only.

## Requirements

- Python 3.10+
- Outbound internet (api.x.com, pbs.twimg.com, graph.threads.net)
- A public HTTPS hostname with no port that X can reach
- One X developer app (webhooks + Activity API)
- One Meta/Threads app (Threads API)

## Documentation

- [AGENTS.md](AGENTS.md) — agent install runbook
- [docs/GOTCHAS.md](docs/GOTCHAS.md) — production failure modes
- [SECURITY.md](SECURITY.md) — secrets, signatures, CRC guard, media allow-list

## Disclaimer

Not affiliated with, endorsed by, or sponsored by X Corp or Meta. "X" and "Threads" are trademarks of their owners. Use at your own risk — automating a personal account may interact with platform terms.

## License

MIT — see [LICENSE](LICENSE).
