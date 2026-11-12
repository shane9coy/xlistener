# GOTCHAS

Production notes from running XListener against the live X and Threads APIs. Every section
below cost someone real time. Each ends with what to check when it bites.

## 1. Zero X credits silently stop webhook delivery

Not in X's docs. Symptom: `GET /2/webhooks` reports `valid:true`, the subscription is
active, CRC passes, the endpoint is healthy — and zero POSTs arrive for hours. Adding
credits resumed delivery in under a minute. An empty balance also returns HTTP 402 on
reads. X bills $0.005 per delivered `post.create` event and per post returned on reads.

**If you see this:** a healthy stack with no inbound POSTs, or any 402 — check the credit
balance first, top up, then watch for `POST accepted` in `mirror.log`.

## 2. A registered webhook delivers nothing without an event subscription

AAA had one `/all` subscription; XAA is per event type. You need exactly `post.create`.

**If you see this:** `valid:true`, CRC green, and no events ever — run
`tools/setup_xaa.py --url https://listen.example.com/webhooks/twitter --apply` and confirm
`event_types=['post.create']`.

## 3. `valid:true` can be stale

X re-runs CRC only when the webhook has not validated in 24h, plus on registration and on
`PUT /2/webhooks/{id}`. So `valid:true` is not proof the current secret still works.

**If you see this:** after rotating a secret, force `PUT /2/webhooks/{id}` (or wait out the
24h cycle) and check the next delivery's `POST accepted`/`401` lines.

## 4. The signature scheme changed

`X-Twitter-Webhooks-Signature-OAuth2` is keyed with the OAuth 2.0 client secret; the legacy
`X-Twitter-Webhooks-Signature` with the OAuth 1.0 consumer secret. An app with only an
OAuth2 secret sends only the OAuth2 header — a legacy-only verifier 401s every delivery. A
stale secret produces a flood of 401s, not silence.

**If you see this:** rejections naming only `X-Twitter-Webhooks-Signature-OAuth2` — set
`X_OAUTH2_CLIENT_SECRET`. `signature headers present=none` — a proxy is stripping headers.

## 5. CRC is the same HMAC, and it is a signing oracle if you let it sign arbitrary input

The CRC reply and the event signature are the same construction. Signing whatever arrives
would let anyone ask the endpoint to sign a body of their choosing, then POST it as a valid
event. Only sign tokens matching a strict opaque pattern.

**If you see this:** a real handshake answered `400 invalid crc_token` — compare the pattern
against a captured `crc_token` rather than loosening it; a JSON object must never match.

## 6. AAA and XAA deliver the same post twice

During overlap both payload formats can arrive on one webhook, and X retries a delivery
that timed out. The listener takes a **claim** in `state.db` before any slow work, so the
second delivery is refused as `already-mirrored` instead of double-posting.

**If you see this:** two attempts for one id — expected; the claim ledger suppresses the
second. Drop the AAA subscription once XAA delivery is confirmed. A row with
`status='error'` is deliberately NOT suppressed: the next delivery retries it, so a
transient Threads failure does not lose the post.

## 7. X delivered an empty `tweet_create_events: []` array

Seen after a credits top-up. There is nothing to mirror; if you do not log event counts the
delivery is invisible.

**If you see this:** `event counts: {'tweet_create_events': 0}` — not your bug; a delivery
arrived with no events. Check the subscription, and if it persists, raise it with X.

## 8. Carousel publishing order matters

Per item: create a child container with `is_carousel_item=true` and no caption, poll it to
`status=FINISHED`. Then create the parent with `media_type=CAROUSEL` and `children` as a
comma-separated id list, caption on the parent. Then
`threads_publish?creation_id=<parent>`. If a publish silently fails, recreate children AND
parent — a retry referencing dead children fails.

**If you see this:** `Media Not Found`/400 on publish — a container was not ready (the
poster retries) or the children are stale; rebuild from the children up.

## 9. Meta fetches `image_url` server-side

It must be public HTTPS. Anonymous image hosts flake and return 0-length bodies, so
HEAD-verify the URL before handing it to Meta.

**If you see this:** `Image Not Found` / `OAuthException code 1` on image posts — HEAD the
URL yourself: it must return `Content-Type: image/*` and non-zero `Content-Length`.

## 10. Threads can return HTTP 200 with a post id for a post that is not actually live

The publish endpoint can report success for an invalid post. Verify by polling
`GET /{post_id}` for a `permalink`, and recreate + publish on failure.

**If you see this:** `[OK] mirrored` but the Threads URL 302s to `?error=invalid_post` —
treat the id as failed, rebuild the container, republish.

## 11. Threads access tokens last 60 days

A dead token looks exactly like a delivery outage: `[ERR post]` on every event.

**If you see this:** every event logging `[ERR post]` — call `GET /{id}?fields=id,username` with
the token; if it fails, re-authorize inside the 60-day window.

## 12. Never write synthetic rows into the claim ledger

The table is keyed by post id and every row means something: `pending` blocks a post for 15
minutes, `ok` suppresses it forever, `error` allows a retry. A fake `ok` row for a real id
means that post never mirrors.

**If you see this:** one specific post never appears — check
`SELECT * FROM mirrored WHERE tweet_id = '<id>'`. Remove a bogus row, or clear genuine
failures with `tools/replay_backlog.py --reset-errors`.

## 13. A webhook `200` no longer means "published"

The listener claims the post, answers X immediately, and publishes on a background task —
because publishing takes longer than X's 10-second delivery timeout. So a `200 {"queued":
true}` means accepted, not on Threads yet. The publish outcome is the `[OK]` / `[ERR …]` line
that follows.

**If you see this:** a `200` with no `[OK]` — read the next log lines. A failure records
`status='error'` and the next delivery of that post retries it.

## 14. Observe mode is only safe because it uploads nothing

`observe_only` used to build the real payloads, which uploaded every image to a public host
before the dry-run check — so "publishes nothing" still leaked media. Dry runs now return the
plan before any upload, take no claim, and write no rows.

**If you see this:** an image host upload while `observe_only` is true — the dry-run return
must stay above `upload_to_public_host()` in `threads_poster.py`.

## 15. Every mirrored image leaves your machine for an anonymous public host

Meta fetches `image_url` server-side, so the poster uploads each photo to uguu / litterbox /
catbox / 0x0 and hands Meg the URL. Only those hosts are allow-listed, redirects are refused,
and the child environment is scrubbed of credentials — but the media itself is public.

**If you see this:** `Uploading … to a public host` in the log. That is by design; if it is
unacceptable, run your own image host and pass `--image-url`.

## 16. Handler-level regressions are invisible without integration tests

A refactor once nested the whole processing block inside an unrelated `if`, so every real
event returned `200 {"processed": []}` and matched nothing — accepted, dropped, logged
nothing. `tests/test_handler_integration.py` posts real payloads at the built app; keep it.

**If you see this:** `POST accepted` followed by `{"processed": []}` for every real post —
run the handler integration tests before touching the endpoint again.
