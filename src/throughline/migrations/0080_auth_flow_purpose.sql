-- Forward-only. Allow OIDC auth flows to claim the first owner account (gated
-- by the one-time setup code) or link an external identity to an authenticated
-- owner account from onboarding/settings.
ALTER TABLE tl.auth_flows
  ADD COLUMN IF NOT EXISTS purpose text NOT NULL DEFAULT 'login'
    CHECK (purpose IN ('login', 'claim_owner', 'link_account')),
  ADD COLUMN IF NOT EXISTS account_id bigint REFERENCES tl.accounts(id) ON DELETE CASCADE,
  ADD COLUMN IF NOT EXISTS handle_hint text;
