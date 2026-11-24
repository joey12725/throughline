-- Compare immutable values directly; converting entire rows (including the
-- generated tsvector) to JSON twice on each numbering update dominated imports.
CREATE OR REPLACE FUNCTION tl.immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF current_setting('throughline.purge',true)='on' AND current_user NOT IN ('throughline_app','throughline_reader') AND session_user NOT IN ('throughline_app','throughline_reader') THEN IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF; END IF;
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'ground truth cannot be deleted'; END IF;
 IF TG_TABLE_NAME!='messages' THEN RAISE EXCEPTION 'ground truth is immutable'; END IF;
 IF ROW(NEW.id,NEW.channel_id,NEW.external_id,NEW.version,NEW.thread_id,NEW.happened_at,NEW.happened_offset_min,NEW.ended_at,NEW.ingested_at,NEW.author_handle,NEW.content,NEW.images,NEW.fields,NEW.access_label,NEW.payload_hash) IS DISTINCT FROM ROW(OLD.id,OLD.channel_id,OLD.external_id,OLD.version,OLD.thread_id,OLD.happened_at,OLD.happened_offset_min,OLD.ended_at,OLD.ingested_at,OLD.author_handle,OLD.content,OLD.images,OLD.fields,OLD.access_label,OLD.payload_hash) THEN RAISE EXCEPTION 'ground truth is immutable'; END IF;
 RETURN NEW;
END $$;
ALTER TABLE tl.audit_log ADD COLUMN http_status int;
