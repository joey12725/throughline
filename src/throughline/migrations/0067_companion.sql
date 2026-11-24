-- Companion devices and the desktop bridge (throughline.companion,
-- docs/companion.md).
--
-- A companion device is a desktop or laptop running the Throughline
-- Companion app. It is enrolled on the box (`throughline lib companion
-- enroll`), which creates its device token: an ordinary plugin token scoped to
-- the plugin name companion_<device>, so it can only report its own health and
-- serve the bridge. The desktop plugins it runs get their own plugin tokens.
--
-- The bridge lets a plugin running on the server ask a device for something
-- only the desktop has (browser cookies, an allow-listed file, a localhost
-- service). The device declares each capability and the plugins allowed to
-- call it; the core refuses any other caller. Requests live here only until
-- they are answered: the answer is cleared as soon as the caller reads it, and
-- unanswered requests expire. No request arguments or answers are audited.

CREATE TABLE tl.companion_devices(
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name text NOT NULL UNIQUE CHECK (name ~ '^[a-z][a-z0-9_]{0,40}$'),
  token_id bigint NOT NULL UNIQUE REFERENCES tl.tokens(id),
  platform text,
  app_version text,
  -- [{name, description, allowed_plugins:[...]}], as last declared by the device
  capabilities jsonb NOT NULL DEFAULT '[]',
  -- [{name, state, last_run_at, last_error}] of the desktop plugins it runs
  plugins jsonb NOT NULL DEFAULT '[]',
  last_seen_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz
);

CREATE TABLE tl.companion_requests(
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  device_id bigint NOT NULL REFERENCES tl.companion_devices(id) ON DELETE CASCADE,
  capability text NOT NULL,
  args jsonb NOT NULL DEFAULT '{}',
  requested_by bigint REFERENCES tl.tokens(id),
  plugin text,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','claimed','done','failed','expired','collected')),
  response jsonb,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  claimed_at timestamptz,
  finished_at timestamptz,
  expires_at timestamptz NOT NULL
);
CREATE INDEX companion_requests_open
  ON tl.companion_requests(device_id,created_at) WHERE status IN ('pending','claimed');

GRANT SELECT,INSERT,UPDATE ON tl.companion_devices TO throughline_app;
GRANT USAGE,SELECT ON SEQUENCE tl.companion_devices_id_seq TO throughline_app;
GRANT SELECT,INSERT,UPDATE,DELETE ON tl.companion_requests TO throughline_app;
GRANT USAGE,SELECT ON SEQUENCE tl.companion_requests_id_seq TO throughline_app;
