CREATE OR REPLACE FUNCTION api.catalog() RETURNS jsonb LANGUAGE sql STABLE AS $$
SELECT jsonb_build_object(
 'channels',(SELECT coalesce(jsonb_agg(to_jsonb(c) ORDER BY c.name),'[]') FROM api.channels c),
 'views',(SELECT jsonb_agg(jsonb_build_object('name',table_name,'description',CASE table_name WHEN 'messages' THEN 'Current immutable message versions, joined to channels, threads and people' WHEN 'message_versions' THEN 'All message versions, including superseded records' WHEN 'relations' THEN 'Identity relationships in both directions' ELSE 'Read-only '||replace(table_name,'_',' ') END,'example','SELECT * FROM api.'||quote_ident(table_name)||' LIMIT 10') ORDER BY table_name) FROM information_schema.views WHERE table_schema='api'),
 'functions',(SELECT jsonb_agg(jsonb_build_object('name',name,'description',description,'example',example)) FROM (VALUES
 ('context','Expand a message within its numbered thread','SELECT * FROM api.context(1,10,5)'),
 ('search','Rank current non-reference messages by keywords','SELECT * FROM api.search(''orchard'',20)'),
 ('search_all','Rank messages including reference channels','SELECT * FROM api.search_all(''orchard'',20)'),
 ('near','Messages within meters of a point','SELECT * FROM api.near(1,2,1000)'),
 ('near_place','Messages near a named place entity','SELECT * FROM api.near_place(''Synthetic place'',1000)'),
 ('overlapping','Messages overlapping a closed time interval','SELECT * FROM api.overlapping(''2020-01-01Z'',''2020-01-02Z'')'),
 ('during','Messages overlapping the time of another message','SELECT * FROM api.during(1)'),
 ('person','Resolve a name, handle or identifier','SELECT * FROM api.person(''fake-account'')'),
 ('person_messages','Current messages authored by a person','SELECT * FROM api.person_messages(1)'),
 ('follow','Follow active links in either direction without cycles','SELECT * FROM api.follow(''message'',1,NULL,2)'),
 ('catalog','Describe the SQL surface','SELECT api.catalog()')
 ) f(name,description,example)),
 'link_types',(SELECT coalesce(jsonb_agg(DISTINCT type),'[]') FROM api.links))
$$;
