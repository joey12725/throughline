CREATE INDEX links_from_endpoint ON tl.links(from_kind,from_id);
CREATE INDEX links_to_endpoint ON tl.links(to_kind,to_id);
CREATE OR REPLACE FUNCTION api.current_message(message_id bigint) RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
 WITH RECURSIVE versions AS (SELECT id,superseded_by FROM tl.messages WHERE id=$1 UNION ALL SELECT m.id,m.superseded_by FROM tl.messages m JOIN versions v ON v.superseded_by=m.id) SELECT id FROM versions WHERE superseded_by IS NULL
$$;
