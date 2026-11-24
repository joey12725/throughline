-- Write contract for agents and plugins (MSG-2, LNK-1,
-- PLG-4, BRN-3, ARC-5, ACL-6; scenarios write_replay and link_vocabulary).
--
-- 1. Idempotency receipts: one row per (token, Idempotency-Key) holding the
--    request hash and the response skeleton (receipts and ids only, never
--    record text). A replay with the same request returns it; a different
--    request under the same key is refused (HTTP 409).
-- 2. Message aliases and per-key deliveries: an earlier or provisional
--    external id of a record resolves to its canonical external id (archive
--    ids never change), and the last payload hash seen under each delivered
--    key keeps alternating sources from re-versioning a record.
-- 3. tl.stored_hash(kind, id): the hash a receipt reports and that
--    compare-and-set (expected_hash) checks, computed from the stored row.
-- 4. Purge hooks.
--
-- Forward-only. tl objects are schema-qualified.

-- 1. Idempotency receipts --------------------------------------------------------
CREATE TABLE tl.write_receipts(
 token_id bigint NOT NULL REFERENCES tl.tokens(id) ON DELETE CASCADE,
 idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 200),
 method text NOT NULL,
 path text NOT NULL,
 request_hash text NOT NULL,
 status_code int NOT NULL,
 response jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz NOT NULL DEFAULT now() + interval '30 days',
 PRIMARY KEY(token_id,idempotency_key));
CREATE INDEX write_receipts_expires ON tl.write_receipts(expires_at);
REVOKE ALL ON tl.write_receipts FROM PUBLIC,throughline_reader;
GRANT SELECT,INSERT,DELETE ON tl.write_receipts TO throughline_app;

-- 2. Message aliases and per-key deliveries ---------------------------------------
CREATE TABLE tl.message_aliases(
 channel_id bigint NOT NULL REFERENCES tl.channels(id) ON DELETE CASCADE,
 alias text NOT NULL CHECK (length(alias) > 0),
 external_id text NOT NULL,
 origin text NOT NULL DEFAULT 'source' CHECK (origin IN ('source','owner')),
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(channel_id,alias),
 CHECK (alias <> external_id));
CREATE INDEX message_aliases_canonical ON tl.message_aliases(channel_id,external_id);
REVOKE ALL ON tl.message_aliases FROM PUBLIC,throughline_reader;
GRANT SELECT,INSERT ON tl.message_aliases TO throughline_app;

CREATE TABLE tl.message_deliveries(
 channel_id bigint NOT NULL REFERENCES tl.channels(id) ON DELETE CASCADE,
 delivered_key text NOT NULL,
 external_id text NOT NULL,
 last_payload_hash text NOT NULL,
 identity_quality text,
 last_seen_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(channel_id,delivered_key));
CREATE INDEX message_deliveries_canonical ON tl.message_deliveries(channel_id,external_id);
REVOKE ALL ON tl.message_deliveries FROM PUBLIC,throughline_reader;
GRANT SELECT,INSERT,UPDATE ON tl.message_deliveries TO throughline_app;

-- 3. Stored hashes ---------------------------------------------------------------
-- sha256 (hex) over a canonical jsonb array of the stored row; NULL when the
-- row does not exist. 'provenance' hashes the provenance of a derived message;
-- 'provenance:<derived_kind>' that of another derived kind.
CREATE FUNCTION tl.stored_hash(kind text, id bigint) RETURNS text LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
DECLARE
 doc jsonb;
 dk text;
BEGIN
 IF stored_hash.kind='message' THEN
  SELECT jsonb_build_array('message',c.name,m.external_id,m.version,m.payload_hash,
          encode(sha256(convert_to(m.content,'UTF8')),'hex'))
    INTO doc FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE m.id=stored_hash.id;
 ELSIF stored_hash.kind='link' THEN
  SELECT jsonb_build_array('link',l.type,l.from_kind,l.from_id,l.to_kind,l.to_id,l.method,l.evidence,
          l.confidence,l.valid_from,l.valid_to,l.valid_from_precision,l.valid_to_precision,l.status,l.superseded_by)
    INTO doc FROM tl.links l WHERE l.id=stored_hash.id;
 ELSIF stored_hash.kind='entity' THEN
  SELECT jsonb_build_array('entity',e.kind,e.name,e.external_ref,e.metadata,e.is_placeholder,e.merged_into)
    INTO doc FROM tl.entities e WHERE e.id=stored_hash.id;
 ELSIF stored_hash.kind='claim' THEN
  SELECT jsonb_build_array('claim',c.statement,c.claim_type,c.attributed_person_id,c.attributed_handle,
          (SELECT coalesce(jsonb_agg(jsonb_build_array(s.message_id,s.version,s.relation) ORDER BY s.message_id,s.version,s.relation),'[]'::jsonb)
             FROM tl.claim_sources s WHERE s.claim_id=c.id),
          c.status,c.superseded_by)
    INTO doc FROM tl.claims c WHERE c.id=stored_hash.id;
 ELSIF stored_hash.kind='annotation' THEN
  SELECT jsonb_build_array('annotation',a.target_channel_id,a.target_external_id,a.target_version,a.attribute,
          a.value,a.method,a.valid_from,a.valid_to,a.status,a.superseded_by)
    INTO doc FROM tl.annotations a WHERE a.id=stored_hash.id;
 ELSIF stored_hash.kind='proposal' THEN
  SELECT jsonb_build_array('proposal',p.kind,encode(sha256(convert_to(p.payload::text,'UTF8')),'hex'),p.status)
    INTO doc FROM tl.proposals p WHERE p.id=stored_hash.id;
 ELSIF stored_hash.kind='person' THEN
  SELECT jsonb_build_array('person',p.handle,p.display_name,p.is_owner,p.status,p.kind,p.merged_into,p.asserted_by)
    INTO doc FROM tl.people p WHERE p.id=stored_hash.id;
 ELSIF stored_hash.kind='identifier' THEN
  SELECT jsonb_build_array('identifier',i.system,i.kind,i.value_norm,i.person_id,i.status,i.asserted_by)
    INTO doc FROM tl.identifiers i WHERE i.id=stored_hash.id;
 ELSIF stored_hash.kind='provenance' OR stored_hash.kind LIKE 'provenance:%' THEN
  dk := coalesce(nullif(split_part(stored_hash.kind,':',2),''),'message');
  SELECT jsonb_build_array('provenance',dk,stored_hash.id,
          jsonb_agg(jsonb_build_array(p.source_message_id,p.source_version,p.relation,p.origin)
            ORDER BY p.source_message_id,p.source_version,p.relation,p.origin))
    INTO doc FROM tl.provenance p WHERE p.derived_kind=dk AND p.derived_id=stored_hash.id
    HAVING count(*)>0;
 ELSE
  RAISE EXCEPTION 'stored_hash: unknown kind %',stored_hash.kind USING ERRCODE='invalid_parameter_value';
 END IF;
 IF doc IS NULL THEN
  RETURN NULL;
 END IF;
 RETURN encode(sha256(convert_to(doc::text,'UTF8')),'hex');
END $$;
REVOKE EXECUTE ON FUNCTION tl.stored_hash(text,bigint) FROM PUBLIC,throughline_reader;
GRANT EXECUTE ON FUNCTION tl.stored_hash(text,bigint) TO throughline_app;

-- 4. Purge (ARC-5): run before the hooks that delete claims (ord 30), while
--    the channel's messages, their links and the claims citing them can still
--    be joined.
INSERT INTO tl.purge_hooks(name,ord,statement) VALUES
 ('write_receipts',15,
  'WITH msgs AS MATERIALIZED (SELECT m.id FROM tl.messages m WHERE m.channel_id=$1),
   lnk AS MATERIALIZED (SELECT l.id FROM tl.links l WHERE (l.from_kind=''message'' AND l.from_id IN (SELECT id FROM msgs)) OR (l.to_kind=''message'' AND l.to_id IN (SELECT id FROM msgs))),
   clm AS MATERIALIZED (SELECT c.id FROM tl.claims c WHERE c.revision_message_id IN (SELECT id FROM msgs)
     OR EXISTS(SELECT 1 FROM tl.claim_sources s WHERE s.claim_id=c.id AND s.message_id IN (SELECT id FROM msgs))),
   ids AS (SELECT jsonb_build_object(
     ''m'',(SELECT coalesce(jsonb_agg(id),''[]''::jsonb) FROM msgs),
     ''l'',(SELECT coalesce(jsonb_agg(id),''[]''::jsonb) FROM lnk),
     ''c'',(SELECT coalesce(jsonb_agg(id),''[]''::jsonb) FROM clm)) AS v)
   DELETE FROM tl.write_receipts r USING ids
    WHERE jsonb_path_exists(r.response,
     ''$.** ? ((@.kind == "message" && @.id == $m) || (@.kind == "link" && @.id == $l) || (@.kind == "claim" && @.id == $c) || @.message_id == $m || @.link_id == $l || @.claim_id == $c)'',
     ids.v)'),
 ('message_aliases',16,'DELETE FROM tl.message_aliases a WHERE a.channel_id=$1'),
 ('message_deliveries',17,'DELETE FROM tl.message_deliveries d WHERE d.channel_id=$1');

-- 5. Catalogue -----------------------------------------------------------------
INSERT INTO tl.catalog_sections(subject,body,version) VALUES
 ('message_aliases',jsonb_build_object(
  'description','Message aliases (feature message_aliases): a message item may carry aliases, up to 10 earlier or provisional external ids of the same record in the same channel. The first external id stays canonical; an alias resolves to it for ingest, links ({channel, external_id} ends) and provenance.',
  'feature','message_aliases',
  'rules',jsonb_build_array(
   'Resolution order: the external_id is a known alias -> the canonical record; a current record exists under external_id -> that record; else an alias naming an existing record -> the delivered id becomes an alias of it (status aliased); when the external_id and an alias are two existing records, one same_as link joins them (status pair_linked) and recall collapses the pair.',
   'aliases are outside the payload hash, so MSG-2 replay equality is unchanged.',
   'Per-key versioning: once a record has an alias, each delivered key keeps the payload hash it last delivered. A key already seen creates a new version only when its own payload changed. A key seen for the first time creates a new version only when its payload differs and its fields.identity_quality ranks strictly higher (THROUGHLINE_IDENTITY_QUALITY_ORDER, default native_id, bridge_id, fingerprint, unknown); otherwise the delivery is only recorded.')),
  1),
 ('write_contract',jsonb_build_object(
  'description','How writes report what they stored: Idempotency-Key header (8-200 characters, per token, 30 days), server-side readback receipts {kind, id, record_id, version, status, stored_hash, counts, written_at, replayed}, compare-and-set (expected_version on ingest items, expected_hash on link, claim, annotation and entity changes) and per-item results for link and entity batches.',
  'rules',jsonb_build_array(
   'Same key and same request: the stored response is returned with receipt.replayed true and nothing is written again. Same key and a different request: HTTP 409.',
   'Stored responses keep receipts, ids, counts and statuses only, never record text.',
   'expected_version 0 means the message must not exist yet; a mismatch is a per-item rejection with reason version_conflict and the current version.',
   'expected_hash is a receipt''s stored_hash; a mismatch is HTTP 409 with the current hash.',
   'POST /v1/links/batch and /v1/entities/batch check authorization for every item first: any authorization failure is 403 for the whole batch with nothing written; then each item runs alone and reports created, existing, folded, candidate, refused or error.'),
  'routes','POST /v1/links/batch {links:[<=1000]}; POST /v1/entities/batch {entities:[<=1000]}; POST /v1/ingest/messages {receipts:true} for per-message receipts (at most 1000 messages).'),
  1);

-- Deleting a message (purge) checks tl.messages.superseded_by references; without
-- an index every deleted row scans the whole table (found purging a live channel).
CREATE INDEX messages_superseded_by ON tl.messages(superseded_by) WHERE superseded_by IS NOT NULL;
