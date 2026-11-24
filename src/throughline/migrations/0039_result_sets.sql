-- Result sets: asker-scoped working sets (QRY-34, QRY-35, ACL-9, ACL-10,
-- LIB-9).
--
-- A result set stores record ids only: prefix, ref_id, the pinned message id
-- and version, span, score, role and operator output that is never record
-- text. Content and visibility always come from the gated record views
-- registered in tl.record_sources, joined live by api.result_set(handle), so a
-- member whose record is hidden now simply does not join, and a purged message
-- takes its members with it (foreign key). Sets are temporary working state,
-- not derived data: they expire (24 hours by default, at most 7 days) and are
-- readable only by the token and asker that wrote them under the access
-- policy version they were written with.
--
-- The reader cannot create tables; the application role writes these tables
-- and agents read them only through api.result_set and api.result_set_info.

-- 1. Bounded operator output ------------------------------------------------
-- Every string (and every object key) in a jsonb value is at most n
-- characters: the column can hold ids, verdicts and short reasons, not text
-- copied from a record.
CREATE FUNCTION tl.short_json(j jsonb,n int) RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
DECLARE k text; v jsonb;
BEGIN
 IF j IS NULL THEN RETURN true; END IF;
 CASE jsonb_typeof(j)
  WHEN 'string' THEN RETURN length(j #>> '{}')<=n;
  WHEN 'object' THEN
   FOR k,v IN SELECT e.key,e.value FROM jsonb_each(j) e LOOP
    IF length(k)>n OR NOT tl.short_json(v,n) THEN RETURN false; END IF;
   END LOOP;
   RETURN true;
  WHEN 'array' THEN
   FOR v IN SELECT e.value FROM jsonb_array_elements(j) e LOOP
    IF NOT tl.short_json(v,n) THEN RETURN false; END IF;
   END LOOP;
   RETURN true;
  ELSE RETURN true;
 END CASE;
END $$;
REVOKE EXECUTE ON FUNCTION tl.short_json(jsonb,int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tl.short_json(jsonb,int) TO throughline_app;

-- 2. Tables -----------------------------------------------------------------
CREATE TABLE tl.sessions(
 id uuid PRIMARY KEY,
 token_id bigint REFERENCES tl.tokens ON DELETE CASCADE,
 asker text,
 policy_version bigint NOT NULL,
 note text CHECK(note IS NULL OR length(note)<=500),
 unresolved jsonb NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(unresolved)='array' AND tl.short_json(unresolved,500) AND octet_length(unresolved::text)<=16384),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 expires_at timestamptz NOT NULL);
CREATE INDEX sessions_token ON tl.sessions(token_id);
CREATE INDEX sessions_expiry ON tl.sessions(expires_at);

CREATE TABLE tl.result_sets(
 handle text PRIMARY KEY CHECK(handle ~ '^rs_[0-9a-f]{32}$'),
 session_id uuid REFERENCES tl.sessions ON DELETE CASCADE,
 token_id bigint REFERENCES tl.tokens ON DELETE CASCADE,
 asker text,
 policy_version bigint NOT NULL,
 origin jsonb NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(origin)='object' AND tl.short_json(origin,500) AND octet_length(origin::text)<=65536),
 completeness text NOT NULL CHECK(completeness IN ('exhaustive','ranked_sample','truncated')),
 counts jsonb NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(counts)='object' AND tl.short_json(counts,100)),
 as_of timestamptz,
 known_by timestamptz,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 expires_at timestamptz NOT NULL);
CREATE INDEX result_sets_token ON tl.result_sets(token_id);
CREATE INDEX result_sets_session ON tl.result_sets(session_id,created_at);
CREATE INDEX result_sets_expiry ON tl.result_sets(expires_at);

CREATE TABLE tl.result_set_members(
 handle text NOT NULL REFERENCES tl.result_sets ON DELETE CASCADE,
 ordinal int NOT NULL CHECK(ordinal>=0),
 prefix text NOT NULL CHECK(prefix ~ '^[a-z]{1,8}$'),
 ref_id bigint NOT NULL,
 record_id text NOT NULL,
 -- The message a member is pinned to: purge deletes the member with it.
 message_id bigint REFERENCES tl.messages ON DELETE CASCADE,
 version int,
 span_first int,
 span_last int,
 score real,
 role text NOT NULL DEFAULT 'hit' CHECK(role ~ '^[a-z_]{1,32}$'),
 depends_on text CHECK(depends_on IS NULL OR depends_on ~ '^[a-z]{1,8}:[0-9]{1,19}$'),
 discovery jsonb NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(discovery)='array' AND tl.short_json(discovery,500) AND octet_length(discovery::text)<=16384),
 -- Expansion refs, verdicts and other operator output; never record text.
 meta jsonb NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(meta)='object' AND tl.short_json(meta,500) AND octet_length(meta::text)<=16384),
 PRIMARY KEY(handle,ordinal),
 CHECK(record_id=prefix||':'||ref_id),
 CHECK(prefix<>'m' OR message_id=ref_id),
 CHECK(span_first IS NULL OR span_last IS NULL OR span_last>=span_first));
CREATE INDEX result_set_members_message ON tl.result_set_members(message_id) WHERE message_id IS NOT NULL;

-- A member may be in several groups, each certainly or tentatively.
CREATE TABLE tl.result_set_groups(
 handle text NOT NULL,
 ordinal int NOT NULL,
 grp text NOT NULL CHECK(length(grp) BETWEEN 1 AND 200),
 certain boolean NOT NULL DEFAULT true,
 PRIMARY KEY(handle,ordinal,grp),
 FOREIGN KEY(handle,ordinal) REFERENCES tl.result_set_members ON DELETE CASCADE);

GRANT SELECT,INSERT,UPDATE,DELETE ON tl.sessions,tl.result_sets,tl.result_set_members,tl.result_set_groups TO throughline_app;

-- 3. Ownership check --------------------------------------------------------
-- The set, when it exists, is unexpired, belongs to the token and asker of the
-- current transaction settings (an empty setting is NULL; a malformed token
-- setting matches nothing), its token is not revoked, and the access policy
-- is the one it was written under. Otherwise no row.
CREATE FUNCTION tl.result_set_usable(handle text) RETURNS SETOF tl.result_sets LANGUAGE sql STABLE SET search_path=pg_catalog,tl AS $$
 SELECT s.* FROM tl.result_sets s
 WHERE s.handle=$1 AND $1 ~ '^rs_[0-9a-f]{32}$'
 AND s.expires_at>now()
 AND s.token_id IS NOT DISTINCT FROM (CASE
  WHEN coalesce(current_setting('throughline.token_id',true),'')='' THEN NULL
  WHEN current_setting('throughline.token_id',true) ~ '^[0-9]{1,18}$' THEN current_setting('throughline.token_id',true)::bigint
  ELSE -1 END)
 AND s.asker IS NOT DISTINCT FROM nullif(current_setting('throughline.asker',true),'')
 AND s.policy_version=api.policy_version()
 AND (s.token_id IS NULL OR EXISTS(SELECT 1 FROM tl.tokens t WHERE t.id=s.token_id AND t.revoked_at IS NULL))
$$;
REVOKE EXECUTE ON FUNCTION tl.result_set_usable(text) FROM PUBLIC;

-- 4. api.result_set ---------------------------------------------------------
-- One branch per registered record view, joined on ref_id (a message member
-- also on its pinned version). The views decide visibility, so a member whose
-- record is hidden from the current asker does not join. A prefix without a
-- registered (or existing) view yields no rows.
CREATE FUNCTION api.result_set(handle text) RETURNS TABLE(
 ordinal int,
 record_id text,
 kind text,
 ref_id bigint,
 message_id bigint,
 version int,
 is_current boolean,
 channel text,
 external_id text,
 thread_id bigint,
 thread_no int,
 content text,
 record_time timestamptz,
 ended_at timestamptz,
 ingested_at timestamptz,
 subject_from timestamptz,
 subject_to timestamptz,
 subject_from_precision text,
 subject_to_precision text,
 person_id bigint,
 author_handle text,
 lat double precision,
 lon double precision,
 loc_reason text,
 is_outside boolean,
 heading_path text[],
 char_start int,
 char_end int,
 claim jsonb,
 generated boolean,
 unsupported boolean,
 provenance jsonb,
 support jsonb,
 expansion jsonb,
 span_first int,
 span_last int,
 score real,
 role text,
 depends_on text,
 groups jsonb,
 discovery jsonb,
 meta jsonb,
 set_completeness text) LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
DECLARE s tl.result_sets; src record; parts text[] := ARRAY[]::text[];
BEGIN
 SELECT * INTO s FROM tl.result_set_usable(result_set.handle) LIMIT 1;
 IF NOT FOUND THEN RETURN; END IF;
 FOR src IN SELECT r.prefix,to_regclass(r.relation)::text AS relation FROM tl.record_sources r
  WHERE r.prefix ~ '^[a-z]{1,8}$' AND r.relation ~ '^api\.[a-z_]+$' AND to_regclass(r.relation) IS NOT NULL
  ORDER BY r.prefix LOOP
  parts := parts || format($q$SELECT x.ordinal,r.record_id::text,r.kind::text,r.ref_id::bigint,r.message_id::bigint,r.version::int,r.is_current::boolean,r.channel::text,r.external_id::text,r.thread_id::bigint,r.thread_no::int,r.content::text,r.record_time::timestamptz,r.ended_at::timestamptz,r.ingested_at::timestamptz,r.subject_from::timestamptz,r.subject_to::timestamptz,r.subject_from_precision::text,r.subject_to_precision::text,r.person_id::bigint,r.author_handle::text,r.lat::double precision,r.lon::double precision,r.loc_reason::text,r.is_outside::boolean,r.heading_path::text[],r.char_start::int,r.char_end::int,r.claim::jsonb,r.generated::boolean,r.unsupported::boolean,r.provenance::jsonb,r.support::jsonb,r.expansion::jsonb,x.span_first,x.span_last,x.score,x.role,x.depends_on,(SELECT coalesce(jsonb_agg(jsonb_build_object('grp',g.grp,'certain',g.certain) ORDER BY g.grp),'[]'::jsonb) FROM tl.result_set_groups g WHERE g.handle=x.handle AND g.ordinal=x.ordinal),x.discovery,x.meta,$2 FROM tl.result_set_members x JOIN %s r ON r.ref_id=x.ref_id%s WHERE x.handle=$1 AND x.prefix=%L$q$,
   src.relation,CASE WHEN src.prefix='m' THEN ' AND r.version=x.version' ELSE '' END,src.prefix);
 END LOOP;
 IF cardinality(parts)=0 THEN RETURN; END IF;
 RETURN QUERY EXECUTE 'SELECT * FROM ('||array_to_string(parts,' UNION ALL ')||') u ORDER BY 1' USING s.handle,s.completeness;
END $$;

-- What the set is, under the same checks. member_count and counts.returned
-- count only the members visible now.
CREATE FUNCTION api.result_set_info(handle text) RETURNS TABLE(
 handle text,
 session_id uuid,
 completeness text,
 counts jsonb,
 origin jsonb,
 as_of timestamptz,
 known_by timestamptz,
 created_at timestamptz,
 expires_at timestamptz,
 member_count bigint) LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 SELECT s.handle,s.session_id,s.completeness,s.counts||jsonb_build_object('returned',n.c),s.origin,s.as_of,s.known_by,s.created_at,s.expires_at,n.c
 FROM tl.result_set_usable($1) s CROSS JOIN LATERAL (SELECT count(*) AS c FROM api.result_set($1)) n
$$;
GRANT EXECUTE ON FUNCTION api.result_set(text),api.result_set_info(text) TO throughline_app,throughline_reader;

-- 5. Catalogue --------------------------------------------------------------
INSERT INTO tl.api_functions(name,kind,signature,description,exactness,cost_class,level,access,example,version,ranked_limit_arg,default_limit) VALUES
 ('result_set','sql','result_set','Members of one of your result sets, joined live to the gated record views: the record contract columns plus ordinal, span, score, role, depends_on, groups, discovery, meta and the set''s completeness. A ranked_sample set is not a population: aggregates over it describe the sample only','exact','free',2,'owner, agent; own sets only (same token and asker, unexpired, same policy version); rows pass the asker gate','SELECT * FROM api.result_set(''rs_00000000000000000000000000000000'')','1',NULL,NULL),
 ('result_set_info','sql','result_set_info','Completeness, counts, origin, as_of, lifetime and the number of visible members of one of your result sets. A ranked_sample set is not a population','exact','free',2,'owner, agent; own sets only; member_count counts visible members','SELECT * FROM api.result_set_info(''rs_00000000000000000000000000000000'')','1',NULL,NULL);
UPDATE tl.api_functions f SET signature=f.name||'('||pg_get_function_arguments(p.oid)||') RETURNS '||pg_get_function_result(p.oid)
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='api' AND p.proname=f.name AND f.name IN ('result_set','result_set_info');

INSERT INTO tl.catalog_sections(subject,body) VALUES
 ('result_sets',jsonb_build_object(
  'description','Result sets are temporary working state: named lists of record ids (handle rs_ followed by 32 hex digits) that recall, transforms, match and plans produce and that SQL reads with api.result_set(handle).',
  'storage','A set stores ids, the pinned message version, span, score, role, groups, discovery and short operator output (meta). It never stores record text: api.result_set joins every member to the gated record view of its kind (tl.record_sources) when it is read, so content and visibility are always current.',
  'ownership','Only the token and asker that created a set can read it, only while the access policy version is unchanged (any change of askers, grants or token askers invalidates it) and only until it expires. Anything else returns no rows; over HTTP an unknown or foreign handle is 404 and a set from an earlier policy is 409.',
  'lifetime','Default 24 hours (THROUGHLINE_RESULT_SET_TTL_S), at most 7 days. Expired sets are deleted by the next write. A purged message removes its members.',
  'completeness',jsonb_build_object('exhaustive','Every record matching the producing operation is a member','ranked_sample','The best-ranked members of a larger population; not a population','truncated','A population cut by a limit or budget'),
  'columns','api.result_set returns ordinal, the record contract columns, span_first, span_last, score, role, depends_on, groups ([{grp, certain}]), discovery, meta and set_completeness.',
  'transforms','POST /v1/sets/transform: union, intersect, subtract (by record_id and version), filter, order, head, sample, dedup, group, ungroup. Each creates a new set whose origin names the operation, parameters and parent handles.',
  'inspect','POST /v1/sets/{handle}/inspect: summary, sample, by_time, by_channel, by_person, groups (manifest), missing_link, origin, history; bounded by a token budget.',
  'example','SELECT r.record_id,m.channel,r.score FROM api.result_set(''rs_00000000000000000000000000000000'') r JOIN api.messages m ON m.id=r.message_id ORDER BY r.ordinal'));
