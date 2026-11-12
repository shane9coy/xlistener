# Security

## Secrets

This project needs live credentials for two platforms. They belong in `.env`
(git-ignored) and nowhere else:

- X: consumer key/secret, access token/secret, app-only bearer token, OAuth 2.0 client secret
- Threads: long-lived access token, numeric user id

**Never** commit `.env`, `config.json`, `state.db`, `mirror.log`, `media_cache/`,
or `.x-budget.json`. If you think a secret was committed, rotate it immediately in
the X Developer Console and the Meta app dashboard, then rewrite history — deleting
the file in a later commit does not remove it from git history.

Useful pre-push check:

```bash
docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:latest detect -s /repo -v
```

## Request size

The webhook endpoint refuses bodies over **1 MiB**: a declared `Content-Length` over the cap
is rejected with `413` before authentication, and a chunked body is cut off mid-stream so an
unauthenticated caller cannot make the process allocate unbounded memory. Put a request-size
limit at the edge too (Cloudflare's own limit is the outer bound) and consider `MemoryMax=` on
the systemd unit.

## Webhook verification

The listener refuses any POST that does not carry a valid HMAC signature over the
**raw** request body, checked in constant time. Two headers are supported:

- `X-Twitter-Webhooks-Signature-OAuth2` — keyed with the OAuth 2.0 client secret (preferred)
- `X-Twitter-Webhooks-Signature` — keyed with the OAuth 1.0 consumer secret (legacy)

Each header is verified only against its own secret. If neither secret is
configured, the listener refuses to start rather than accepting unsigned traffic.

## CRC oracle guard

The CRC endpoint returns `base64(HMAC-SHA256(secret, crc_token))`. If it signed
arbitrary input, it would be a signing oracle: an attacker could ask it to sign a
JSON body and then POST that body with a valid signature — which would publish to
your Threads account. The listener therefore only signs tokens matching a strict
opaque-token pattern, which no JSON object can satisfy.

## Media allow-list

Photos are only downloaded from X's own image CDN: https only, host suffix `.twimg.com`,
no userinfo, no backslashes or `%5c`, and redirects are not followed. A payload cannot make
the listener fetch an arbitrary URL, and a parser-differential URL cannot redirect the fetch.

## Media egress (disclosure)

Publishing requires Meta to fetch each image over HTTPS. The poster uploads mirrored images to
one of four anonymous public hosts — `uguu.se` (temporary), `litterbox.catbox.moe` (24h),
`catbox.moe` (permanent), `0x0.st` (24h) — and only URLs on an allow-list are handed to Meta.
Treat every mirrored image as public content. If that is unacceptable, self-host and pass an
`image_url` you control.

## Child processes

The Threads poster runs as a subprocess with a minimal environment (`PATH`, `HOME`, locale,
TLS settings) rather than the parent's, so credentials exported into the listener's shell are
not inherited by it or by its `curl` uploads. The poster loads its own `.env` into a private
dict and dry runs print `"<token>"` instead of the real credential.

## Reporting

Open an issue for anything non-sensitive. For a credential leak or a verification
bypass, contact the maintainer privately rather than filing a public issue.
