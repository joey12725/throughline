-- Retain immutable native detection history while excluding retired derived
-- stops from the current visits surface.
CREATE OR REPLACE VIEW api.visits AS SELECT m.* FROM api.messages m LEFT JOIN tl.raw_payloads p ON p.message_id=m.id WHERE m.channel='visits' AND (p.payload->'retired') IS DISTINCT FROM 'true'::jsonb;
