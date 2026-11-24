# Web interface: signing in

How a browser gets into the interface. Everything here is optional: the
bearer token on `/v1` keeps working exactly as before, and a deployment that
configures nothing still offers a username and password (once an account
exists) and pasting an access token.

| Way in | What the browser holds | Configured by |
|---|---|---|
| External provider (Authentik, any OpenID Connect) | session cookie | `THROUGHLINE_AUTH_PROVIDERS` and the provider's variables |
| Username and password | session cookie | an account with a password (`THROUGHLINE_AUTH_PASSWORD=0` hides it) |
| Access token | the token, in session or local storage | `throughline token create` (`THROUGHLINE_AUTH_TOKEN=0` hides the option on the screen) |

## The model: accounts point at principals

An **account** (`tl.accounts`) is someone who signs in. It carries no
permissions of its own. Each account points at one `tl.tokens` row, its
**principal**, and a signed-in browser's requests run exactly as that token's
would: same kind, askers, limits, gate, `tl.web_state` namespace and audit.
The audit row also records `detail.account_id` and `detail.via` (`password` or
the provider id).

How someone proves they are an account is kept apart from the account:

- `tl.account_passwords`: at most one local password (scrypt, N=2^15).
- `tl.account_links`: any number of external identities, keyed by
  `(provider, subject)`, where `provider` is the configured id.

So a new provider never changes accounts, and an account can have a password
and several providers at once.

Today every account is created with a fresh **owner** principal: it sees and
does what the owner does. The direction this leaves open, without building it:

- **Several people, different scopes.** An account whose principal is an
  `agent` token restricted to an asker gets that asker's information scope
  through the existing database gate, with its own `asker:<name>` namespace
  for saved queries and boards. Only account creation changes.
- **Profiles.** `tl.accounts.person_id` can say which person in the archive
  the account is, for "me"-relative views.
- **Provider-driven roles.** Group claims are already read (for
  `ALLOWED_GROUPS`); mapping a group to a principal is a policy on top of the
  same tables.

## Accounts are made on the box

As with tokens, the browser never creates or changes accounts. All commands use
the owner database connection:

```sh
throughline lib account create alex --name "Alex" --email alex@example.com --password
throughline lib account list
throughline lib account password alex            # set; --remove for provider-only
throughline lib account link alex --provider authentik --subject <sub>
throughline lib account unlink alex --provider authentik
throughline lib account disable alex             # also ends its sessions
throughline lib account sessions [alex]
throughline lib account sign-out alex            # or --session ID, or --all
```

Changing or removing a password ends that account's password sessions;
unlinking a provider ends the sessions it opened; disabling ends all of them.

## Sessions

A successful sign-in sets an HttpOnly, `SameSite=Lax` cookie holding a random
secret; only its SHA-256 is stored (`tl.web_sessions`). The cookie is named
`__Host-tl_session` and marked `Secure` when the interface is served over
https, `tl_session` otherwise.

- "Stay signed in on this device" makes the cookie persistent for
  `THROUGHLINE_AUTH_SESSION_DAYS` (default 30). Otherwise it is a browser
  session cookie and the server ends it after
  `THROUGHLINE_AUTH_SESSION_HOURS` (default 12).
- Sign-out (`POST /v1/auth/logout`) revokes the row and clears the cookie.
- A bearer token in the request wins over a cookie.

**Cross-site requests.** The API answers no CORS preflight, so another site
cannot add a custom header. A cookie-authenticated request that changes
something must carry `X-Throughline-Client` (the interface always sends it),
and any cookie-authenticated request a browser marks `Sec-Fetch-Site:
cross-site` or `same-site` is refused. The sign-in and sign-out routes require
the header on every request, which also blocks login CSRF.

**Password throttle.** Five wrong passwords per account and twenty per client
address within 15 minutes answer 429 with `Retry-After`. A wrong username
costs as much time as a wrong password, and both answer the same 401.

## OpenID Connect and Authentik

Authorization code flow with PKCE (S256), `state` bound to the browser by a
short-lived cookie, and a `nonce`. The ID token comes straight from the token
endpoint over TLS with this server authenticated as the client, so its
signature is not checked (OpenID Connect Core 3.1.3.7 step 6); issuer,
audience, authorised party, expiry and nonce are.

**Egress.** The server talks only to the configured issuer's host (discovery
and token exchange; no record data is ever sent). A discovered endpoint on any
other host must be in `THROUGHLINE_EGRESS_ALLOW`, and every endpoint must be
https unless it is loopback.

**Which account.** A sign-in maps to the account linked to `(provider, sub)`.
On the first sign-in, if `LINK_BY_EMAIL` is on (the default) and the provider
says the e-mail is verified, an account with that e-mail (and no identity from
this provider yet) is linked automatically. Otherwise the screen shows the
subject and the exact `account link` command to run. `ALLOWED_GROUPS` limits
sign-in to members of the listed groups (the `groups` claim).

### Authentik setup

1. In Authentik, create an **OAuth2/OpenID Provider**: client type
   *Confidential*, redirect URI
   `https://<throughline-host>/v1/auth/oidc/authentik/return` (strict), signing
   key any, scopes `openid`, `email`, `profile` (the default *profile* mapping
   includes `groups`).
2. Create an **Application** using it (slug, say, `throughline`) and bind the
   users or groups who may use it.
3. Configure Throughline:

```sh
THROUGHLINE_PUBLIC_URL=https://throughline.example.com
THROUGHLINE_AUTH_PROVIDERS=authentik
THROUGHLINE_AUTH_AUTHENTIK_ISSUER=https://auth.example.com/application/o/throughline/
THROUGHLINE_AUTH_AUTHENTIK_CLIENT_ID=...
THROUGHLINE_AUTH_AUTHENTIK_CLIENT_SECRET=...
# optional
THROUGHLINE_AUTH_AUTHENTIK_ALLOWED_GROUPS=throughline-users
```

4. `throughline lib account create alex --email <your Authentik e-mail>`, then
   use "Continue with Authentik".

### Other providers

Any OpenID Connect provider works with `KIND=oidc`, under any id:

```sh
THROUGHLINE_AUTH_PROVIDERS=authentik,pocket
THROUGHLINE_AUTH_POCKET_KIND=oidc
THROUGHLINE_AUTH_POCKET_LABEL=Pocket ID
THROUGHLINE_AUTH_POCKET_ISSUER=https://id.example.com
THROUGHLINE_AUTH_POCKET_CLIENT_ID=...
THROUGHLINE_AUTH_POCKET_CLIENT_SECRET=...
```

A new provider *family* with its own defaults is one entry in
`auth/config.py` `PRESETS`; a non-OIDC protocol is a module beside
`auth/oidc.py` with its own `/v1/auth/<protocol>/<id>/start` and `/return`.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `THROUGHLINE_AUTH_PASSWORD` | `1` | `0` hides username and password sign-in and refuses `POST /v1/auth/login` |
| `THROUGHLINE_AUTH_TOKEN` | `1` | `0` hides "Use an access token" on the screen (bearer tokens still work on `/v1`) |
| `THROUGHLINE_AUTH_PROVIDERS` | unset | Comma-separated provider ids |
| `THROUGHLINE_AUTH_<ID>_ISSUER` | — | Issuer URL (required) |
| `THROUGHLINE_AUTH_<ID>_CLIENT_ID` | — | Required |
| `THROUGHLINE_AUTH_<ID>_CLIENT_SECRET` | unset | Confidential client secret (HTTP Basic at the token endpoint) |
| `THROUGHLINE_AUTH_<ID>_KIND` | the id if it is a preset, else `oidc` | `authentik` or `oidc` |
| `THROUGHLINE_AUTH_<ID>_LABEL` | from the preset | Button text: "Continue with …" |
| `THROUGHLINE_AUTH_<ID>_SCOPES` | `openid email profile` | |
| `THROUGHLINE_AUTH_<ID>_ALLOWED_GROUPS` | unset | Only members of one of these groups |
| `THROUGHLINE_AUTH_<ID>_LINK_BY_EMAIL` | `1` | `0`: identities are linked only with `account link` |
| `THROUGHLINE_AUTH_<ID>_REDIRECT_URI` | derived | Override the callback URL |
| `THROUGHLINE_PUBLIC_URL` | from the request | The address browsers use; set it behind a reverse proxy |
| `THROUGHLINE_AUTH_SESSION_DAYS` | `30` | Lifetime of a remembered session |
| `THROUGHLINE_AUTH_SESSION_HOURS` | `12` | Lifetime of any other session |
| `THROUGHLINE_AUTH_COOKIE_SECURE` | `auto` | `auto`: Secure when the public URL (or the request) is https |

A provider with missing or invalid settings is left out and named in a warning
at startup; the others still work.

## Routes

Public, outside the OpenAPI document, never audited (no principal yet):

```
GET  /v1/auth/config                      -> {methods: {password, token}, providers: [{id, kind, label, start}], accounts}
GET  /v1/auth/session                     -> {account: null} | {account, session: {method, persistent, created_at, expires_at}, principal: {kind, restricted}}
POST /v1/auth/login   {username, password, remember}   -> as /session, sets the cookie; 401, 429, 404 when disabled
POST /v1/auth/logout                      -> {signed_out: true}, clears the cookie
GET  /v1/auth/oidc/{id}/start?return_to=/ui/...&remember=bool   -> 303 to the provider
GET  /v1/auth/oidc/{id}/return            -> 303 to return_to, or to /ui/#auth_error=<code>&provider=<id>[&subject=<sub>]
```

`return_to` is honoured only for paths under `/ui`. Error codes the screen
explains: `denied`, `expired`, `unlinked`, `not_permitted`, `disabled`,
`unreachable`, `provider` (details in the server log).

| Piece | Where |
|---|---|
| Settings, presets | `src/throughline/auth/config.py` |
| Accounts (owner connection) | `src/throughline/auth/accounts.py`, CLI `src/throughline/cli_commands/accounts.py` |
| Sessions, password sign-in | `src/throughline/auth/sessions.py`, `auth/passwords.py` |
| OpenID Connect | `src/throughline/auth/oidc.py` |
| Routes | `src/throughline/api/auth_routes.py`; cookie resolution in `api/deps.py` `authenticate` |
| Migration | `src/throughline/migrations/0072_accounts.sql` |
| Screen | `web/src/app/SignInScreen.tsx`, `web/src/store/auth.ts`, `web/src/api/auth.ts` |
| Tests | `tests/test_web_auth.py` |
