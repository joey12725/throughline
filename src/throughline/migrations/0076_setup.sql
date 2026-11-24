-- Onboarding (throughline.onboarding, docs/onboarding.md).
--
-- setup_state: one row, the owner's progress through the web onboarding.
-- plugin_repos: third-party plugin repositories added by GitHub link (the
--   bundled starter set is not a row).
-- plugin_installs: plugins installed or scheduled by onboarding: where the
--   program is, its scoped token, and the recurring-sync schedule. Settings
--   and secrets are not here; they live in owner-only files on the box.
-- setup_jobs: installs, backfills, syncs and people runs, with progress the
--   browser polls.
-- merge_questions: people the resolver could not decide on, as questions
--   for the owner, and its own decisions for the summary.
-- setup_kv: small onboarding documents (e.g. the resolver's summary).

CREATE TABLE tl.setup_state(
  id int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  status text NOT NULL DEFAULT 'fresh' CHECK (status IN ('fresh','in_progress','done')),
  mode text NOT NULL DEFAULT 'fresh' CHECK (mode IN ('fresh','restore','migrate')),
  steps jsonb NOT NULL DEFAULT '{}',
  choices jsonb NOT NULL DEFAULT '{}',
  started_at timestamptz,
  completed_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tl.setup_kv(
  key text PRIMARY KEY,
  value jsonb NOT NULL DEFAULT '[]',
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tl.plugin_repos(
  id text PRIMARY KEY,
  name text NOT NULL,
  url text NOT NULL,
  ref text,
  commit text,
  path text NOT NULL,
  description text NOT NULL DEFAULT '',
  added_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tl.plugin_installs(
  plugin_id text PRIMARY KEY CHECK (plugin_id ~ '^[a-z][a-z0-9_]*$'),
  repo_id text NOT NULL,
  path text,
  command jsonb,
  token_id bigint REFERENCES tl.tokens(id),
  enabled boolean NOT NULL DEFAULT false,
  interval_s int NOT NULL DEFAULT 3600 CHECK (interval_s > 0),
  next_run_at timestamptz,
  last_run_at timestamptz,
  last_status text,
  last_error text,
  installed_at timestamptz,
  backfilled_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tl.setup_jobs(
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind text NOT NULL CHECK (kind IN ('install','backfill','sync','people','check','lens','pages')),
  plugin_id text,
  title text NOT NULL,
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','running','done','failed','cancelled','interrupted')),
  phase text,
  progress jsonb NOT NULL DEFAULT '{}',
  error jsonb,
  log_tail jsonb NOT NULL DEFAULT '[]',
  args jsonb NOT NULL DEFAULT '{}',
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX setup_jobs_plugin ON tl.setup_jobs(plugin_id, id DESC);
CREATE INDEX setup_jobs_open ON tl.setup_jobs(status) WHERE status IN ('queued','running');

CREATE TABLE tl.merge_questions(
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ident_a bigint NOT NULL REFERENCES tl.identifiers(id),
  ident_b bigint NOT NULL REFERENCES tl.identifiers(id),
  person_a bigint REFERENCES tl.people(id),
  person_b bigint REFERENCES tl.people(id),
  confidence real NOT NULL,
  reasons jsonb NOT NULL DEFAULT '[]',
  prompt text NOT NULL,
  suggested_name text,
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','same','different','skipped')),
  answered_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (ident_a < ident_b),
  UNIQUE (ident_a, ident_b)
);

GRANT SELECT,INSERT,UPDATE,DELETE ON tl.setup_state,tl.setup_kv,tl.plugin_repos,tl.plugin_installs,
  tl.setup_jobs,tl.merge_questions TO throughline_app;
GRANT USAGE,SELECT ON SEQUENCE tl.setup_jobs_id_seq,tl.merge_questions_id_seq TO throughline_app;
