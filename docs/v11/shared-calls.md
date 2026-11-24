# Shared model calls

When several jobs ask the same model about the same input, Throughline sends the
input once and asks all their questions in that one call. The input (a day of
messages, a video candidate, a recording) is usually most of a request, so each
extra question costs only its own prompt and answer instead of a whole new call.
This applies to subscription models too, where it saves quota and time.

Nothing has to be configured. Sharing happens in the maintenance tick
(`throughline lib lens tick`, `throughline lib classifiers tick`). A run you start
by hand, a backfill and a rule-triggered run still call on their own.

## Lenses

Module `throughline.lenses.coalesce`; the calls are `workers.run_shared_llm` and
`workers.run_shared_jev`.

**Which lenses share.** Lenses can share a call when they use the same worker
(`llm` or `jev`), the same endpoint and model, and the same channels and grain.
Each item is then checked: the item part of the request must be identical for
both lenses (`workers.share_fingerprint`):
- **LLM lens:** the rendered messages and the date calendar.
- **Jev lens:** the candidate and its `item_context`.

Two day lenses with different `max_chars_per_item` split a busy day differently,
so they share only the days that come out the same. A lens with
`include_fields = false` renders messages differently, so it never shares with
one that includes fields.

**One call.** The first lens to reach an item asks for every sibling whose own
next run would read that item.
- **LLM call:** `tables` holds each lens's prompt, columns, natural key,
  `context_query_results` and owner corrections. The items and `date_context`
  appear once. The reply is `{"tables": {"<lens>": {"rows": [...]}}}`, validated
  against each lens's own schema. If one lens's part fails validation, only
  that lens is asked again (once), just as a solo call would be.
- **Jev call:** the state holds the candidate once, and each lens's policy,
  context and corrections sit under `state.lenses.<name>`. Each question is
  asked as `<lens>__<question>`. A merged payload that would pass Jev's 32 KB
  limit is not shared; each lens calls alone.

Each lens gets its answer as if it had made the call itself: the same rows, the
same `same_as_row_id` merges and the same `min_confidence` filtering.
- **Cost:** tokens and cost are split evenly between the lenses in
  `tl.model_ledger` (`table.<name>`). The call is counted once, on the lens that
  made it.
- **Records:** a run that used shared calls records
  `detail.shared = {shared_calls, saved_tokens, shared_with}` in
  `tl.table_runs`. The tick output carries `shared` totals.

**Schedules.** Lenses have different schedules: `calendar_candidates` runs every
2 hours, `media_recommendations` every 6. When a lens is due and a sibling is
not, the sibling *rides along*:
1. It joins the call for items its own next run would read.
2. Right after the due lens finishes, it runs on the answers it was given, with
   no model calls (`rode_along` in the tick output).
3. It stops at the first item with no answer and reads the rest at its own
   scheduled run.

A rider joins an item only when it also has answers for every earlier item of
its run, so every answer it is given gets used. A lens that is held back
(backoff after an outage, the monthly cap, a dependency that has not finished)
never rides.

`worker_config.coalesce`:

| Value | Effect |
|---|---|
| `true` (default) | share calls and ride along |
| `"due"` | share calls only on ticks where this lens is due anyway |
| `false` | never share |

The older `run_batch` path (lenses with identical schedules) now makes the same
kind of shared call.

## Classifiers

**Message classifiers.** In a tick, Jev message classifiers that judge the same
message with the same model, and render it the same way (the same
`context_messages`), share one Jev call. The first classifier to reach the
message asks each sibling's question too, as `c<classifier id>`. The sibling
finds its answer kept when its own run reaches that message.

A sibling joins only for a message its run this tick will judge: past its
cursor, inside its `max_per_run`, at least its `min_chars` long, not held from
egress, and within its `monthly_cap_usd`. Tokens and cost are split evenly in
the ledger (`classifier.<name>`). The tick output carries
`shared = {shared_calls, answers_given, answers_used}`.

**Python classifiers.** `ctx.jev.ask_many(about, {key: question | (question,
options)})` asks several questions about the same text in one call. It returns
`Answer` for yes/no questions and `Choice` for questions with options. The
`is_it_media` example asks the media level and the media kind in one call. The
kind question used to be a second call, made for every recording with media.

## What does not share

- **Pages:** each page reads different evidence.
- **Librarian:** agent loops with tools.
- **LLM message classifiers:** each one is a free-form instruction with its own
  schema.
- **Lenses with different models, endpoints or item input:** sharing would
  change their answers.
