CREATE FUNCTION tl.refresh_channel(channel_name text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
DECLARE c tl.channels; f jsonb; cols text := ''; typ text; vname text;
BEGIN
 SELECT * INTO STRICT c FROM tl.channels WHERE name=channel_name;
 vname := 'ch_'||replace(c.name,'.','_');
 IF EXISTS(SELECT 1 FROM tl.channels WHERE name<>c.name AND 'ch_'||replace(name,'.','_')=vname) THEN RAISE EXCEPTION 'channel view name collision'; END IF;
 FOR f IN SELECT * FROM jsonb_array_elements(c.fields) LOOP
 typ := CASE f->>'type' WHEN 'text' THEN 'text' WHEN 'integer' THEN 'bigint' WHEN 'number' THEN 'double precision' WHEN 'boolean' THEN 'boolean' WHEN 'timestamp' THEN 'timestamptz' WHEN 'json' THEN 'jsonb' ELSE NULL END;
 IF typ IS NULL THEN RAISE EXCEPTION 'invalid field type'; END IF;
 cols := cols||format(', (fields%s%L)::%s AS %I',CASE WHEN typ='jsonb' THEN '->' ELSE '->>' END,f->>'name',typ,f->>'name');
 END LOOP;
 EXECUTE format('DROP VIEW IF EXISTS api.%I',vname);
 EXECUTE format('CREATE VIEW api.%I AS SELECT m.* %s FROM api.messages m WHERE channel=%L',vname,cols,c.name);
 EXECUTE format('GRANT SELECT ON api.%I TO throughline_app,throughline_reader',vname);
END $$;
REVOKE ALL ON FUNCTION tl.refresh_channel(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tl.refresh_channel(text) TO throughline_app;
