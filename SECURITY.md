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

Photos are only downloaded from X's own image CDN (hosts ending `.twimg.com`, https
only). A payload cannot make the listener fetch an arbitrary URL.

## Reporting

Open an issue for anything non-sensitive. For a credential leak or a verification
bypass, contact the maintainer privately rather than filing a public issue.
