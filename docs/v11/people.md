# People

Migration `0047_people.sql`, module `throughline.people`, routes in
`api/routes/people.py`, commands `throughline lib person ...`.

The person-centric read model answers "what is going on with X" in one call
and then lets an agent drill in. It is built on Throughline's one identity
registry (`tl.people`, `tl.person_names`, `tl.identifiers`); there is no
second registry or crosswalk.

**Start with the brief.** `api.person_brief(person_id)` (`GET
/v1/people/{id}/brief`, `lib person brief ID`) is the default first step for
any agent.

## Attribution guarantee

Only a **confirmed** author identifier sets `tl.messages.person_id`.

- A `BEFORE INSERT OR UPDATE OF person_id, author_identifier_id` trigger on
  `tl.messages` clears `person_id` when the author identifier is suspected or
  ignored. This holds for every writer: ingest, reparse, attach, merge, split.
- An `AFTER UPDATE OF status, person_id` trigger on `tl.identifiers`
  re-attributes that identifier's messages in one statement: its person when
  confirmed, none otherwise.
- The migration cleared `person_id` once where the author identifier was not
  confirmed (`tl.clear_unconfirmed_attribution`) and recorded an identity event
  `clear_unconfirmed_attribution` with `messages_cleared`. `person_id` is
  identity state, not content: the immutable payload is untouched.

So suspected accounts are excluded from every tool (legacy `api.search*` and
`api.person_messages`, recall, SQL, person functions) until the owner
confirms them, and names alone never link a message to a person. The owner
changes a status with `POST /v1/identity/identifiers/{id}/status {status:
confirmed | suspected | ignored, reason?}` (owner tokens only; `lib person
status ID confirmed`). It writes an `identifier_status` identity event with
the actor, the previous and new status and the reason, takes an
`Idempotency-Key` and returns a receipt when the write contract is installed.
`api.suspected_attributions(message_ids)` shows the suspected person behind
messages, for unrestricted askers only.

## api.appearances: attribution on every row

One gated row (predicate `api.row_visible`) per current `life_event` or
`derived` record:

| column | meaning |
| --- | --- |
| `kind` | `turn` for whole-thread channels (recordings), else `message` |
| `record_time`, `effective_time` | record time; the annotated effective time, else record time |
| `person_id` | confirmed attribution only |
| `is_me` | attributed to the owner, or `fields.direction = 'outgoing'` |
| `party_id` | the owner's person id when `is_me`, else `person_id`: the id every person function uses to place a message on a side |
| `annotated_person_id` | the annotated speaker (unrestricted askers only) |
| `direction` | `outgoing` when is_me; `incoming` when attributed to someone else; `fields.direction` when attribution is missing; else `unknown` |
| `attribution_status` | `confirmed`, `self_direction` (is_me from direction only), `annotated` (speaker annotation; unrestricted only), `suspected`, `unknown` |
| `identity_quality` | `fields.identity_quality`, else `identifier` for a confirmed identifier, else `unknown` |
| `time_precision` | `fields.time_precision`, else the subject precision, else `exact` |
| `conversation_kind` | `fields.conversation_kind` (`dm` marks a one-to-one thread) |
| `ignored` | the annotated ignore state |

`api.attribution(message_ids)` returns `{is_me, direction, attribution_status,
identity_quality, time_precision}` per message (any version; the current
version of the same source key answers). The shared hydrator calls it once
per hydrate, so every record of recall, result sets, expand, match, plans and
packets carries `record.attribution` (null for non-message records and for
reference or page records). The result-set `filter` op takes
`identity_quality`, `time_precision` and `attribution_status` lists (one
`api.attribution` statement over the message members; non-message members are
kept only when none of these keys is given). These filters are not part of
recall's hard scope (its SQL signature is fixed).

## The owner side

Your side is decided by `is_me`: your attribution **or** `direction
'outgoing'`. So "me and X" works even where your own account identifier is
not confirmed (or not attached): `between([owner, X])`, `circle(owner)`,
`ties(owner, X)` and `dms_with(X)` all count your outgoing messages.

## Exchanges

Message R **responds to** message T when

- an active `reply_to` link goes from R to T (any gap), or
- R is the next turn after T in the same thread, by consecutive `thread_no`
  among current, non-ignored versions, within
  `THROUGHLINE_EXCHANGE_WINDOW_S` (default 1800 s, inclusive),

and R and T have different authors: the owner side is any message with
`is_me`; otherwise the attributed person when both are attributed, else
`author_handle`. An ignored message is skipped (the next turn after it
answers the one before).

Replies are resolved once into `tl.exchanges` (keyed by the source keys of
both messages, so new versions keep their rows) with `tl.exchange_state` per
thread (`indexed_to` = the newest `ingested_at` the rebuild saw). New
messages, reply_to link changes, attribution changes, ignore annotations,
thread-order maintenance and an owner change mark threads stale.
`people.refresh(conn, budget_ms)` rebuilds stale threads set-based, one thread
per transaction. Routes refresh within `THROUGHLINE_PEOPLE_REFRESH_MS`
(default 2000 ms) before running a function; threads still stale are
disclosed as `execution.incomplete` `{stage: exchange_index, reason:
exchange_index_stale}` with the warning `exchange_index_stale:<count>` and
`execution.exchange_index`. SQL callers read `api.exchange_index_status()`
(`stale_threads`, `indexed_threads`, `indexed_to`, `indexed_at`, `window_s`,
over the threads they may read). `lib person index` refreshes and shows it.

## Contact

For a person X:

- **from**: messages by X (`party_id = X`);
- **to**: owner-side messages in X's one-to-one threads (`conversation_kind
  dm`, or party ids exactly {owner, X}), and messages by others that respond
  to X;
- **exchanges**: exchanges with X on either end.

## Functions

Times are effective times; `after_at` is inclusive and `before_at`
exclusive; `max_results` NULL means no limit; ignored records are excluded.
Every function reads gated views, so a restricted asker's counts, ranks and
lists cover granted rows only.

| function | route | what |
| --- | --- | --- |
| `person_brief(person_id)` | `GET /v1/people/{id}/brief` | handle, names, identifier counts by system and status, facts, relations (unrestricted only); first and last contact, messages from and to, exchanges, top channels, open loops, circle rank among the owner's ties, recent threads, coverage of the channels involved |
| `person_timeline(person_id, after_at, before_at, max_results)` | `/timeline` | from and to messages, newest first (`side`) |
| `person_arc(person_id, grain)` | `/arc` | The person's arc: per period (week, month, quarter, year; UTC) from, to and exchanges over the whole history, with `first_period`, `peak_period` (most from + to; earliest on ties), `last_period`, `last_contact_at` and `gap_days` |
| `dms_with(person_id, ...)` | `/dms` | both sides of the one-to-one threads (`side` from, to, other) |
| `between(persons, ...)`, `between_threads(persons)` | `GET /v1/between?persons=` | threads where every listed person (at least two) has a message by party_id; messages by any listed person in them |
| `last_contact(person_id)` | `/last-contact` | last from, last to, last exchange, last contact: the newest relevant record, never the newest record of a shared thread |
| `open_loops(person_id, older_than, horizon_days, max_results, ask_patterns, closing_patterns)` | `/open-loops`, `GET /v1/open-loops` | unanswered asks, either direction |
| `interactions(person_id, after_at, before_at, grain)` | `/interactions` | per day, week or month: from, to, exchanges, channels |
| `around(at, window_s, persons, max_results, include_ignored)` | `GET /v1/around` | records across channels (transcripts included) within `window_s` seconds, closest first, `offset_s` |
| `day(on_date, tz, persons, max_results, include_ignored)` | `GET /v1/day` | records of one local day in time order |
| `together(persons, ...)` | `GET /v1/together?persons=` | episodes (the `api.episodes_of` grain) where every listed person has a message |
| `mentions(person_id, ...)` | `/mentions` | messages naming the person (handle, display name, names) as a whole word (names made only of stop words, such as Will, by pattern), not by the person; `matched_term` for unrestricted askers only |
| `circle(person_id, mode, after_at, before_at, max_results)` | `/circle` | ties of a person (default the owner) |
| `ties(a, b, after_at, before_at)` | `GET /v1/people/ties?a=&b=` | the pair breakdown |
| `rhythm(person_id, after_at, before_at, tz)` | `/rhythm` | messages by ISO weekday (1 = Monday) x hour, and by month of the year |

### Circle and ties

`strength = exchanges + shared_conversations`, where exchanges are counted in
both directions between the two party ids and shared conversations are the
distinct episodes where both have messages in the window.
`relative_strength = strength / the sum of strength over the person's ties in
the window` (NULL when that sum is 0, never a division error). Rank by
strength (mode `relative`: relative_strength), then person id, so ranks are
deterministic. `ties(a, b)` gives `exchanges`, `a_responded`, `b_responded`,
`shared_conversations`, `shared_threads`, `strength` and the first and last
exchange.

### Open loops

An **ask** is a message that ends with `?` or matches the request lexicon
(`THROUGHLINE_ASK_PATTERNS`: a JSON list of POSIX regular expressions, or
entries separated by `;;`; case-insensitive). It is **open** when it is older
than `older_than` (default 1 day) and no message by the other party follows
it in the thread. `direction` is `they_owe` (you asked) or `owner_owes` (they
asked). Wound-down chats are excluded: the asker's own later closing or
acknowledgement message (`THROUGHLINE_CLOSING_PATTERNS`, same format), or no
activity in the thread for `horizon_days` (default 30). With a person, the
threads are those you share with them plus their one-to-one threads with
you, even where you never wrote; without, every thread with a message of
yours plus every such one-to-one thread (migration 0053). A one-to-one
thread has a visible message of the person and is `conversation_kind` dm,
or holds only your messages and theirs. With a person, every thread of a
one-to-one channel (only you and that person write in it) counts too, so an
ask of yours alone in its thread has them as counterpart. Group threads and
threads with unattributed authors count only where you wrote. Every
decision reads gated rows, so hidden rows never change which threads are
read.

## Recall: person_role between

`HardScope.person_role` takes `between` (at least two persons, else 400).
`scope_sql.resolve` intersects `thread_ids` with `api.between_threads(persons)`
(no shared thread: an empty response with the warning `no_shared_threads`) and
passes the role to SQL as `involved` restricted to the messages whose
party_id is a listed person, from one `api.appearances` statement. Over
`THROUGHLINE_SCOPE_SOURCE_CAP` (default 5,000) messages, or under `as_of` /
`known_by`, it falls back to role `author` within the shared threads with the
warning `between_author_fallback`. So recall's between equals `api.between`;
`author` stays author-only and `involved` stays thread involvement.

## Restricted askers

They get message-derived person facts (counts, ranks, timelines, arc, open
loops) over granted rows only. Identity data (handles, names, identifiers,
facts, relations, suspected mappings, speaker annotations, matched terms) is
NULL or empty for them. Caller predicates never run below the gate, so
division-by-zero and cast probes over these functions raise nothing.

## Units

| name | unit |
| --- | --- |
| `window_s`, `gap_s`, `offset_s` | seconds |
| `older_than` | interval (`'1 day'`) |
| `horizon_days`, `age_days`, `gap_days` | days (`age_days`, `gap_days` with two decimals) |
| `strength`, `exchanges`, `shared_conversations` | counts |
| `relative_strength` | fraction 0..1 (six decimals) |
| periods | UTC calendar periods (`2020-03-02`, `2020-W10`, `2020-03`, `2020-Q1`, `2020`) |
| `tz` | a time zone name; default `THROUGHLINE_TIMEZONE`, else UTC |

## Purge

A purge hook deletes exchanges whose responder or target is in the purged
channel; `tl.exchange_state` rows go with the threads.
