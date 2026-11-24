-- A fixed search_path prevents earthdistance's SQL body from failing when
-- pg_restore builds indexes with its deliberately empty search_path.
CREATE FUNCTION api.earth_point(lat float8,lon float8) RETURNS public.earth LANGUAGE sql IMMUTABLE PARALLEL SAFE SET search_path=pg_catalog,public AS $$ SELECT public.ll_to_earth(lat,lon) $$;
DROP INDEX tl.messages_earth;
CREATE INDEX messages_earth ON tl.messages USING gist(api.earth_point(lat,lon)) WHERE lat IS NOT NULL;
CREATE OR REPLACE FUNCTION api.near(lat double precision,lon double precision,meters double precision) RETURNS SETOF api.messages LANGUAGE sql STABLE AS $$ SELECT m.* FROM api.messages m WHERE m.lat IS NOT NULL AND earth_box(api.earth_point(lat,lon),meters) @> api.earth_point(m.lat,m.lon) AND earth_distance(api.earth_point(lat,lon),api.earth_point(m.lat,m.lon))<=meters $$;
CREATE OR REPLACE FUNCTION api.person_messages(person_id bigint) RETURNS SETOF api.messages LANGUAGE sql STABLE AS $$ SELECT m.* FROM api.messages m WHERE m.person_id=person_messages.person_id OR EXISTS(SELECT 1 FROM api.links l WHERE l.status='active' AND l.type IN ('author','recipient','participant','present') AND ((l.from_kind='message' AND l.from_id=m.id AND l.to_kind='person' AND l.to_id=person_messages.person_id) OR (l.to_kind='message' AND l.to_id=m.id AND l.from_kind='person' AND l.from_id=person_messages.person_id))) $$;
GRANT EXECUTE ON FUNCTION api.earth_point(float8,float8) TO throughline_app,throughline_reader;
