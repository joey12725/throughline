-- Forward-only. Accounts and browser sessions (docs/web/auth.md).
--
-- An account is someone who signs in to the web interface. It does not carry
-- permissions of its own: it points at one tl.tokens row, its principal, and
-- every request of a signed-in browser runs exactly as that token would (kind,
-- askers, limits, audit). Today accounts are created on the box with an owner
-- token; a later account with an agent token restricted to an asker gets that
-- asker's information scope with no change to the gate, the audit or the
-- interface's namespaces.
--
-- How someone proves they are the account is separate from the account:
--   tl.account_passwords   at most one local password (scrypt)
--   tl.account_links  any number of external identities, keyed by the
--                          configured provider and the identity's subject
-- so providers are added without touching accounts.
--
-- tl.web_sessions: one row per signed-in browser. The cookie carries a random
-- secret; only its SHA-256 is stored. tl.auth_flows: the short-lived state of
-- an external sign-in between the redirect out and the callback.
--
-- None of these tables is visible to the reader role or through schema api.
-- No purge hook: no row holds text derived from records.
CREATE TABLE tl.accounts(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 handle text NOT NULL UNIQUE CHECK (handle ~ '^[a-z0-9][a-z0-9._-]{0,62}$'),
 display_name text,
 email text,
 token_id bigint NOT NULL REFERENCES tl.tokens(id),
 person_id bigint REFERENCES tl.people(id) ON DELETE SET NULL,
 status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
 created_at timestamptz NOT NULL DEFAULT now(),
 last_login_at timestamptz);
CREATE UNIQUE INDEX accounts_email ON tl.accounts(lower(email)) WHERE email IS NOT NULL;
COMMENT ON TABLE tl.accounts IS 'Who signs in to the web interface. Permissions come from token_id (the principal), never from the account itself.';

CREATE TABLE tl.account_passwords(
 account_id bigint PRIMARY KEY REFERENCES tl.accounts(id) ON DELETE CASCADE,
 hash text NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE tl.account_links(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 account_id bigint NOT NULL REFERENCES tl.accounts(id) ON DELETE CASCADE,
 provider text NOT NULL CHECK (provider ~ '^[a-z][a-z0-9_]{0,39}$'),
 subject text NOT NULL CHECK (length(subject) BETWEEN 1 AND 255),
 issuer text,
 email text,
 username text,
 linked_at timestamptz NOT NULL DEFAULT now(),
 last_login_at timestamptz,
 UNIQUE (provider, subject));
CREATE INDEX account_links_account ON tl.account_links(account_id);

CREATE TABLE tl.web_sessions(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 secret_hash text NOT NULL UNIQUE,
 account_id bigint NOT NULL REFERENCES tl.accounts(id) ON DELETE CASCADE,
 method text NOT NULL,
 persistent boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT now(),
 last_seen_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz NOT NULL,
 revoked_at timestamptz,
 user_agent text CHECK (length(user_agent) <= 300));
CREATE INDEX web_sessions_account ON tl.web_sessions(account_id) WHERE revoked_at IS NULL;

CREATE TABLE tl.auth_flows(
 state_hash text PRIMARY KEY,
 provider text NOT NULL,
 nonce text NOT NULL,
 verifier text NOT NULL,
 return_to text NOT NULL,
 persistent boolean NOT NULL DEFAULT false,
 redirect_uri text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now());

GRANT SELECT,INSERT,UPDATE,DELETE ON tl.accounts,tl.account_passwords,tl.account_links,tl.web_sessions,tl.auth_flows TO throughline_app;
