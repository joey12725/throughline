-- Column names take precedence over SQL function argument names. Positional
-- references ensure the center is the caller's location, not each row's own.
CREATE OR REPLACE FUNCTION api.near(lat double precision,lon double precision,meters double precision)
RETURNS SETOF api.messages LANGUAGE sql STABLE AS $$
  SELECT m.* FROM api.messages m
  WHERE m.lat IS NOT NULL
    AND earth_box(api.earth_point($1,$2),$3) @> api.earth_point(m.lat,m.lon)
    AND earth_distance(api.earth_point($1,$2),api.earth_point(m.lat,m.lon)) <= $3
$$;
