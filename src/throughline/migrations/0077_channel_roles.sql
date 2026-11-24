-- Channel roles (throughline.channel_roles, docs/plugin-http-contract.md).
--
-- A plugin may declare per channel what it holds (role), its ordering in
-- POST /v1/orient (orient_priority, higher first) and the smallest thread
-- window POST /v1/find reads around a hit (context_window_min). All three are
-- optional; NULL is neutral. Core features select channels by role, never by
-- name. api.channels is rebuilt so the new columns are readable through the
-- same asker gate (a restricted asker lists only granted channels).

ALTER TABLE tl.channels
 ADD COLUMN role text CHECK (role IS NULL OR role ~ '^[a-z][a-z0-9_]{0,39}$'),
 ADD COLUMN orient_priority int CHECK (orient_priority IS NULL OR orient_priority BETWEEN -1000 AND 1000),
 ADD COLUMN context_window_min int CHECK (context_window_min IS NULL OR context_window_min BETWEEN 0 AND 40);

SELECT tl.install_gate('channels',NULL,'SELECT * FROM tl.channels');
