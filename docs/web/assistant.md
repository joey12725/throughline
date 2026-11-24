# The query helper: HTTP contract

The helper beside the query editor is a bounded, read-only tool loop that
runs in the API process. Its product is always a `(statement, view spec)`
pair the owner can see, edit, run and save. This file is the contract
between `src/throughline/assistant/` and `web/src/features/query/`.

## Configuration

| Variable | Meaning |
|---|---|
| `THROUGHLINE_ASSISTANT_PROVIDER` | `anthropic`, `openai` (any OpenAI-compatible endpoint, including local servers) or `local` (deterministic, no model). Default `local`. |
| `THROUGHLINE_ASSISTANT_MODEL` | Model id. Anthropic default `claude-opus-5-5`. |
| `THROUGHLINE_ASSISTANT_BASE_URL` | Endpoint for `openai` (and an override for `anthropic`). |
| `THROUGHLINE_ASSISTANT_API_KEY` | Key; falls back to `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`. |
| `THROUGHLINE_ASSISTANT_TIER` | Disclosure tier, `schema` or `samples`. Default `samples` for the local helper (or a model declared local), `schema` for every other model, a loopback endpoint included. `samples` here also allows it for a non-loopback endpoint. |
| `THROUGHLINE_ASSISTANT_MODEL_IS_LOCAL` | `1`: the owner states that the loopback endpoint's model runs on this machine (not a gateway to a vendor). Only then are sensitivity labels skipped and `samples` the default. Ignored for an endpoint that is not loopback. |
| `THROUGHLINE_ASSISTANT_MAX_STEPS` | Tool steps per run, default 8. |
| `THROUGHLINE_ASSISTANT_MAX_USD` | Spend ceiling per run, default 0.50. A run also stays within the token's `max_model_cost` (default 0.10), which holds for the whole run. |
| `THROUGHLINE_ASSISTANT_DAILY_USD` | Helper spend one token may reach in any 24 hours, from the ledger; default 5.00. |

Requests go through `providers.http`, so the endpoint's host must be in
`THROUGHLINE_EGRESS_ALLOW`; spend is written to the provider ledger. Each
turn's `max_tokens` is lowered so that its worst case fits what the run (and
the token's day) may still spend; a run that cannot afford another turn ends
with an answer that says which limit was reached.

What the model sees: at `schema` names, types, counts and errors; at
`samples` also clipped rows. Unless the model runs on this machine (the
local helper, or `THROUGHLINE_ASSISTANT_MODEL_IS_LOCAL=1`), sensitivity
labels that keep content on the machine withhold a statement's rows whole,
profile ranges and top values, and recall hits of held records. Database
errors reach the model as their SQLSTATE plus, for class 42 (syntax and
names) only, the message with every quoted text that is not a name from the
statement removed; any other class is a fixed description, never the
database's message (it can quote a stored value). See docs/web/backend.md.

`GET /v1/web/bootstrap` → `features.assistant`:

```json
{"provider": "anthropic", "model": "claude-opus-5-5", "loopback": false,
 "default_tier": "schema", "tiers": ["schema", "samples"], "configured": true}
```

`provider: "local"` means no model: the panel still works as a finder
(recipes, channels, fields, people, full text) and Fix uses a rule table.

## Start a run

`POST /v1/web/assistant` (owner or asker token; plugin tokens 403)

```json
{
  "verb": "ask | fix | explain | refine",
  "question": "how did my sleep change after I moved?",
  "statement": "SELECT …",            // current editor text (fix/explain/refine)
  "selection": "…",                    // selected text (explain/refine), optional
  "error": {"code": "42703", "message": "…"},   // fix
  "result": {"columns": [{"name": "day", "type": "date"}], "row_count": 120,
             "truncated": false},       // shape only, never rows
  "tier": "schema | samples",          // optional; refused 400 if above what the server allows
  "scope": {"from": "…", "to": "…", "tz": "America/New_York"},
  "channel": "health.oura.daily",      // optional focus
  "history": [{"role": "user|assistant", "text": "…"}]   // ≤ 10 prior turns, ≤ 8 KB
}
```

Answer `202`:

```json
{"run": "r_…", "status": "running", "provider": "anthropic",
 "model": "claude-opus-5-5", "tier": "schema"}
```

Validation errors are 400/422, never 5xx. At most 4 active runs per token,
and a bounded number in the process (429 with `Retry-After`); a cancelled run
counts until its worker has returned.

## Follow a run

`GET /v1/web/assistant/{run}?after=<seq>&wait=<0..25>` long-polls: it answers
as soon as there is an event after `after`, or the run ends, or `wait`
seconds pass.

```json
{
  "run": "r_…", "status": "running | done | failed | cancelled",
  "provider": "anthropic", "model": "claude-opus-5-5", "tier": "schema",
  "events": [
    {"seq": 1, "at": "…", "kind": "step", "tool": "describe",
     "input": {"subject": "health.oura.daily"}, "summary": "34 fields",
     "sent": "field names, types, descriptions (2.1 KB)", "ms": 41},
    {"seq": 2, "kind": "step", "tool": "run_sql", "sql": "SELECT …",
     "row_count": 120, "columns": [...], "error": null,
     "sent": "column names and types, 120 rows withheld (schema tier)", "ms": 88},
    {"seq": 3, "kind": "text", "text": "Sleep score fell about 6 points…"},
    {"seq": 4, "kind": "final"}
  ],
  "final": {
    "answer": "markdown",
    "statement": "SELECT …",
    "spec": { "$schema": "throughline.view/1", … } ,   // or null
    "notes": ["Assumes the move date is 2025-03-14"]
  },
  "usage": {"tokens_in": 5120, "tokens_out": 830, "cost_usd": 0.041, "steps": 3},
  "error": null
}
```

`POST /v1/web/assistant/{run}/cancel` → `{"status": "cancelled"}`.

Runs live in process memory for 30 minutes; a restarted server answers 404.
Only the token that started a run can read or cancel it.

## Tools (all read-only, all under the request's scope)

| Tool | Does |
|---|---|
| `describe(subject)` | catalogue: channels, typed columns, declared fields with descriptions, api functions, allow-listed builtins |
| `profile(channel)` | aggregate statistics (counts, ranges, top values) |
| `find_recipes(text)` / `recipe(name)` | the recipe library and adaptation notes |
| `run_sql(statement)` | `/v1/sql` through the unchanged validator and cost gate; the model sees columns, row count and errors, plus clipped rows only at `samples` tier |
| `recall(text)` | keyword/semantic recall, clipped at `samples`, counts only at `schema` |
| `chart(statement, lens, encode)` | proposes a view spec |
| `final(answer, statement, spec, notes)` | ends the run |

Every step is reported as an event with what was sent to the model, so the
panel can show it.
