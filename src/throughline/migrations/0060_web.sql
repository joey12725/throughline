-- Forward-only. State of the web interface (docs/web/backend.md).
--
-- tl.web_state holds the interface's own documents: preferences, colour
-- assignments, lens overrides, saved queries, boards and import mappings.
-- A document is what the owner typed or arranged in the interface (a title,
-- SQL text, a layout); the server never copies record content into it and the
-- interface never stores result rows or excerpts in it.
--
-- namespace: '' is the installation's shared namespace, read and written by
-- every unrestricted scope (the owner's interface is the same on every
-- device and with every full-read token). A restricted asker has a namespace
-- of its own, 'asker:<name>', and never sees another one. The route derives
-- the namespace from the request's scope; it is never taken from the request.
--
-- No purge hook: rows are not keyed by channel and hold no text derived from
-- messages (map-build 3.4: a hook is for tables that can hold derived text).
-- A saved query that names a purged channel simply stops returning rows.
-- No seed rows, so the per-test truncate needs no keep-list entry.
--
-- No index on tl.messages is added. Measured on the 186k-row development
-- corpus: keyset browsing of one 120k-row channel view (ORDER BY happened_at
-- DESC, id DESC LIMIT 100) takes about 240 ms without and 220 ms with
-- tl.messages(channel_id, happened_at DESC, id DESC) WHERE superseded_by IS
-- NULL, because the channel views join the channel by name and the planner
-- keeps its top-N sort either way; the arrival tail already runs on the
-- primary key and messages_arrival.
CREATE TABLE tl.web_state(
 namespace text NOT NULL DEFAULT '',
 kind text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_]{0,39}$'),
 key text NOT NULL CHECK (length(key) BETWEEN 1 AND 200),
 value jsonb NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now(),
 token_id bigint REFERENCES tl.tokens(id) ON DELETE SET NULL,
 PRIMARY KEY (namespace,kind,key),
 CHECK (octet_length(value::text) <= 1048576));
COMMENT ON TABLE tl.web_state IS 'Documents of the web interface (preferences, saved queries, boards, ...), per namespace: '''' shared by unrestricted scopes, asker:<name> for a restricted asker. Never record content.';
GRANT SELECT,INSERT,UPDATE,DELETE ON tl.web_state TO throughline_app;
