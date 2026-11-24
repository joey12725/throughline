-- Source health: outages, stalls and catch-up (throughline.health,
-- throughline.cadence, docs/v11/sources.md).
--
-- tl.plugins gains run bookkeeping (last attempt, last error time, failures in
-- a row), a pause switch for sources switched off on purpose, and the state
-- last alerted. tl.source_outages keeps one row per outage: when the data was
-- last known good, when the outage was noticed, the errors seen, when the
-- source came back and what the first run after it caught up. No content is
-- stored: errors are the redacted, truncated text the SDK sends.

ALTER TABLE tl.plugins
  ADD COLUMN IF NOT EXISTS last_attempt_at timestamptz,
  ADD COLUMN IF NOT EXISTS last_error_at timestamptz,
  ADD COLUMN IF NOT EXISTS consecutive_failures integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS paused_at timestamptz,
  ADD COLUMN IF NOT EXISTS paused_reason text,
  ADD COLUMN IF NOT EXISTS alerted_state text;

CREATE TABLE tl.source_outages(
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  plugin text NOT NULL,
  -- failing: runs reach the plugin and fail; silent: no run reported at all;
  -- stalled: runs succeed but a channel's silence is overdue (cadence)
  kind text NOT NULL CHECK (kind IN ('failing','silent','stalled')),
  last_good_at timestamptz,
  detected_at timestamptz NOT NULL DEFAULT now(),
  first_error text,
  last_error text,
  failures integer NOT NULL DEFAULT 0,
  restored_at timestamptz,
  caught_up_messages bigint,
  caught_up_to timestamptz,
  catch_up text CHECK (catch_up IN ('caught_up','partial')),
  catch_up_reasons text[] NOT NULL DEFAULT '{}',
  CHECK (restored_at IS NULL OR restored_at >= detected_at)
);
CREATE UNIQUE INDEX source_outages_open ON tl.source_outages(plugin) WHERE restored_at IS NULL;
CREATE INDEX source_outages_plugin ON tl.source_outages(plugin,detected_at DESC);

GRANT SELECT,INSERT,UPDATE ON tl.source_outages TO throughline_app;
GRANT USAGE,SELECT ON SEQUENCE tl.source_outages_id_seq TO throughline_app;

-- Channels switched off on purpose (a sub-feed that ended): their cadence is
-- still shown, but never makes the source stalled.
CREATE TABLE tl.channel_pauses(
  channel_id bigint PRIMARY KEY REFERENCES tl.channels(id) ON DELETE CASCADE,
  reason text,
  paused_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT,INSERT,UPDATE,DELETE ON tl.channel_pauses TO throughline_app;

-- Cadence detection reads each channel's newest item and its recent weeks.
CREATE INDEX IF NOT EXISTS messages_channel_happened
  ON tl.messages(channel_id,happened_at) WHERE superseded_by IS NULL;
