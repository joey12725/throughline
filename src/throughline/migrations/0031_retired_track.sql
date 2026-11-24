-- Track points are immutable ground truth, so a wrong point (or a whole wrong
-- source) is withdrawn rather than deleted. Only the database owner can write
-- this table (`throughline track retire`); runtime roles, and therefore every
-- HTTP token, can only read it. Attachment and stop detection ignore retired
-- points, and retiring re-attaches or clears the messages that used them.
CREATE TABLE tl.retired_track(
 track_id bigint PRIMARY KEY REFERENCES tl.location_track ON DELETE CASCADE,
 retired_at timestamptz NOT NULL DEFAULT now(),
 reason text
);
GRANT SELECT ON tl.retired_track TO throughline_app;
