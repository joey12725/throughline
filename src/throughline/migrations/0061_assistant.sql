-- The query helper of the web interface (docs/web/assistant.md) is a model
-- provider kind of its own: 'assistant' joins the kinds of tl.provider_state
-- (so api.describe('egress') shows whether the helper's model leaves the
-- host) and the origins of tl.model_ledger (so its spend is attributable).
-- Runs live in process memory; no table holds what the helper saw or said.
ALTER TABLE tl.provider_state DROP CONSTRAINT IF EXISTS provider_state_kind_check;
ALTER TABLE tl.provider_state ADD CONSTRAINT provider_state_kind_check
 CHECK(kind IN ('embedding','rerank','semantic','controller','assistant'));
ALTER TABLE tl.model_ledger DROP CONSTRAINT IF EXISTS model_ledger_origin_check;
ALTER TABLE tl.model_ledger ADD CONSTRAINT model_ledger_origin_check
 CHECK(origin IN ('query','worker','job','rule','curator','librarian','assistant'));
