DROP FUNCTION api.search(text,int);
DROP FUNCTION api.search_all(text,int);
CREATE VIEW api.search_results AS SELECT m.*,0::real AS score FROM api.messages m WHERE false;
CREATE FUNCTION api.search(query text,max_results int DEFAULT 20) RETURNS SETOF api.search_results LANGUAGE sql STABLE AS $$ SELECT m.*,ts_rank_cd(m.tsv,websearch_to_tsquery('english',query)) AS score FROM api.messages m WHERE NOT m.is_reference AND m.tsv @@ websearch_to_tsquery('english',query) ORDER BY score DESC,m.id LIMIT max_results $$;
CREATE FUNCTION api.search_all(query text,max_results int DEFAULT 20) RETURNS SETOF api.search_results LANGUAGE sql STABLE AS $$ SELECT m.*,ts_rank_cd(m.tsv,websearch_to_tsquery('english',query)) AS score FROM api.messages m WHERE m.tsv @@ websearch_to_tsquery('english',query) ORDER BY score DESC,m.id LIMIT max_results $$;
GRANT SELECT ON api.search_results TO throughline_reader,throughline_app;
GRANT EXECUTE ON FUNCTION api.search(text,int),api.search_all(text,int) TO throughline_reader,throughline_app;
