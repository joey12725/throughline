# Asker scope

Package `throughline.access`, `throughline.cli_commands.access`, migrations
`0033_asker_gate.sql` and `0051_disclosure.sql` (sensitivity labels and
disclosure grants, see "Sensitivity labels and disclosure (0051)" below).
Requirements ACL-1, ACL-2, ACL-3 (label policies), ACL-4 (subset), ACL-5,
ACL-7, ACL-8, ACL-9 (base rule), ACL-10, FLT-3; design priority P9.

An *asker* is a name a request reads on behalf of (header
`X-Throughline-Asker`). What an asker may read is decided in the database, in
every reader transaction, by gate functions that read four transaction
settings. Python code never filters rows for visibility.

## Settings

`query.reader_transaction(scope)` is the only place that opens a reader
connection, and it always places:

| Setting | Values | Meaning |
|---|---|---|
| `throughline.scope_mode` | `owner`, `asker`, `deny` | how the gate decides |
| `throughline.asker` | asker name or empty | the asker for mode `asker` |
| `throughline.unknown_asker` | `unrestricted`, `deny` | what an asker without a `tl.askers` row reads |
| `throughline.token_id` | token id or empty | for traces and approvals |

A connection that places none of them (a direct database connection, the
`app` role, existing tests) reads everything. Any mode value this version does
not know is treated as `deny`. The reader role cannot change these settings
from inside a statement: `set_config` and `SET` are rejected by the SQL
validator, and the reader lacks the privilege to call `set_config`.

## Semantics per token kind and asker state

| Token | Asker header | Asker state | Scope | Reads |
|---|---|---|---|---|
| owner | none | - | owner | everything |
| owner | named | registered `owner` | asker | everything |
| owner | named | registered `restricted` | asker | granted channels and threads |
| owner | named | not registered | asker, `unknown_asker` from `THROUGHLINE_UNKNOWN_ASKER` (default `unrestricted`) | everything, or nothing when the variable is `deny` |
| plugin | - | - | owner | everything (plugin routes are write routes) |
| agent, empty askers list | none | - | owner | everything (the legacy full-read token) |
| agent, askers list | none, list has one entry | binds that entry | asker, `unknown_asker=deny` | as the asker's state below |
| agent, askers list | none, list has several | - | 403 | - |
| agent, askers list | not on the list | - | 403 | - |
| agent, askers list | on the list | registered `owner` | asker | everything |
| agent, askers list | on the list | registered `restricted` | asker | granted channels and threads |
| agent, askers list | on the list | not registered | asker, `unknown_asker=deny` | nothing |
| any other kind | - | - | deny | nothing |

Routes that run as the `app` role read below the gate, so every one of them
refuses a restricted scope with 403 before any lookup (`deps.require_unrestricted`).
The exceptions are the caller's own working state.

## What a restricted asker reads

| View | Visible row |
|---|---|
| `api.message_versions`, `api.messages`, every `api.ch_*` | `api.row_visible(id, channel_id, thread_id)`: a grant on the channel or the thread |
| `api.threads` | a grant on the thread or its channel |
| `api.conversations` | a grant on the conversation's thread or that thread's channel |
| `api.channels` | a grant on the channel or on one of its threads (the channel is listed, its other threads are not) |
| `api.message_locations` | the message is visible |
| `api.links` | at least one end is a message, every message end is visible, and when `evidence->'message_ids'` is a non-empty array every id in it is a visible message (anything that is not an integer id fails closed) |
| `api.record_versions`, `api.records` (when migration 0032 is present) | the record's message and thread are visible |
| `api.people`, `person_names`, `identifiers`, `facts`, `facts_history`, `relations`, `relations_history`, `not_same`, `entities`, `location_track`, `plugins` | none (predicate `false`: unrestricted askers only) |

Functions built on these views (`api.search`, `search_all`, `search_filtered`,
`context`, `near`, `overlapping`, `person_messages`, `follow`, `catalog`,
`search_meaning`, `search_fused`, `during`, `records_asof`, `describe`)
inherit the gate. Most of them are SECURITY DEFINER; that does not widen
them, because the gate decides from the transaction settings, not from the
role. Because `api.entities` (and so `api.places`) is unrestricted-only,
`api.near_place` and the `place` filter of the search functions find nothing
for a restricted asker until M6 adds an entity policy. Counts, ranking and limits are computed over visible rows
only: `api.search(q, 10)` returns the top ten the asker may see, not the
visible part of the global top ten. `api.current_message(id)` answers NULL for
a hidden message exactly as for a missing one.

Gate functions (schema `api`, not on the query allow-list):

- `api.asker_unrestricted()`: mode missing or empty, or `owner` -> true;
  `deny` -> false; `asker` -> the registered mode is `owner`, or, without a
  row, `unknown_asker` is not `deny`.
- `api.asker_sees(channel_id, thread_id)`, `api.row_visible(message_id,
  channel_id, thread_id)`: a grant for the current asker. Migration 0036
  replaces `row_visible` (same signature) with the source-inheritance rule;
  0051 replaces both (same signatures) with the label and expiry rule.
- `api.message_visible(message_id)`, `api.sources_visible(ids bigint[])`: an
  empty or NULL list is visible to unrestricted askers only.
- `api.policy_version()`, `api.describe_access()`: the latter describes the
  current asker only (scope mode, asker, its mode, names of granted channels,
  number of granted threads, the rule text, policy version); it never names
  another asker, an ungranted channel, or a total.

## Policy version (ACL-10)

`tl.policy_state.version` moves by one on every statement that changes
`tl.askers`, `tl.asker_grants`, `tl.channel_labels` or `tl.thread_labels`, on
any update of `askers` or `revoked_at` of `tl.tokens`, and when a label kind
that a stored message's `access_label` names is added or removed. Since 0051
`api.policy_version()` also counts expired grants (see "Expiry and the
monotonic version" below). `access.register_asker` and `access.grant` skip
writes that would change nothing, so repeating them does not move the
version. Result sets, caches and approvals store the version they were made
under and are refused when it has moved.

## The gate and why it has two branches

`tl.install_gate(view, predicate, definition)` (owner only) builds:

```
tl.<view>_all    the definition
tl.<view>_gated  WITH (security_barrier) SELECT * FROM tl.<view>_all v WHERE <predicate>
api.<view>       SELECT * FROM tl.<view>_all   WHERE     (SELECT api.asker_unrestricted())
                 UNION ALL
                 SELECT * FROM tl.<view>_gated WHERE NOT (SELECT api.asker_unrestricted())
```

The `(SELECT ...)` is an init plan evaluated once, so exactly one branch runs.
An unrestricted asker runs the plain branch: caller predicates are pushed into
the table scan and the `messages_search` GIN index is used as before. A
restricted asker runs the barrier branch: the planner may not evaluate any
caller expression below the barrier, so a probe such as
`WHERE 1/(CASE WHEN content LIKE '%x%' THEN 0 ELSE 1 END)=1` or a failing cast
only ever sees visible rows and cannot report on hidden ones through an error
(the error oracle through `POST /v1/sql`).

A UNION ALL view is not automatically updatable, so `tl.install_gate` also
puts an INSTEAD OF trigger (`read_only`, function `tl.gated_view_read_only()`)
on every gated api view. A write on it is then a privilege question, as it
was before the gate: the runtime roles hold only SELECT and get
`insufficient_privilege` before the trigger is reached, and a role that does
hold the privilege gets the same error from the trigger.

The predicate is stored in `tl.gates`; `tl.install_gate(view)` with no
predicate re-uses it. A definition may only append columns (CREATE OR REPLACE
VIEW rules); dependents such as `api.messages` and the `ch_` views stay bound
because the api view is replaced in place.

## Rule for later migrations

Every api view that exposes per-record columns is created or changed through
`tl.install_gate`, never with a bare `CREATE VIEW api.*`:

- its `_all` definition reads tl tables or other `tl.*_all` views, never a
  gated api view (the gate would be applied twice and the barrier would block
  push-down for unrestricted askers);
- the predicate refers to the row as `v` and schema-qualifies every object;
- derived rows without a source proof get predicate `false`;
- to add columns, pass the new definition and no predicate;
- functions over these views are `SECURITY INVOKER` (the default) so they run
  under the caller's settings.

## Commands and Python

Owner database connection only; runtime roles can read the policy tables
(the app role) or nothing (the reader) and can write none of them.

```
throughline lib asker add NAME [--mode restricted|owner]
throughline lib asker remove NAME
throughline lib asker grant NAME (--channel CHANNEL | --thread THREAD_ID)
    [--labels L1,L2] [--purpose TEXT] [--expires ISO-TIME]
throughline lib asker extend NAME (--channel CHANNEL | --thread THREAD_ID) --expires (ISO-TIME | never) [--purpose TEXT]
throughline lib asker revoke NAME (--channel CHANNEL | --thread THREAD_ID) [--purpose TEXT]
throughline lib asker list

throughline lib access label LABEL (--channel CHANNEL | --thread THREAD_ID) [--purpose TEXT]
throughline lib access unlabel LABEL (--channel CHANNEL | --thread THREAD_ID) [--purpose TEXT]
throughline lib access labels
throughline lib access kind LABEL [--description TEXT]
throughline lib access egress LABEL (--allow | --deny) [--purpose TEXT]    (encryption.md)
throughline lib access grant|extend|revoke ...     (as under asker)
throughline lib access grants [--asker NAME]
throughline lib access events [--asker NAME] [--limit N]
```

`throughline.access`: `register_asker(name, mode, actor=None)`,
`remove_asker(name, actor=None)`,
`grant(asker, channel=None, thread_id=None, labels=(), purpose=None,
expires_at=None, actor=None)`, `extend(asker, expires_at, channel=None,
thread_id=None, purpose=None, actor=None)`, `revoke(asker, channel=None,
thread_id=None, purpose=None, actor=None)`, `list_askers()`,
`list_grants(asker=None)`, `policy_version()`; labels: `label(label,
channel=None, thread_id=None, purpose=None, actor=None)`, `unlabel(...)`,
`list_labels()`, `add_label_kind(label, description="", purpose=None,
actor=None)`; audit: `access_events(asker=None, limit=100)`. Each raises
`ValueError` for an unknown asker, channel, thread or label, a target naming
both or neither, a revoke or extend of a grant not held, an unlabel of a label
not present, or an expiry that is not an ISO 8601 time (a time without an
offset is UTC). `list_askers()` keeps its 0033 shape; `list_grants()` carries
labels, purpose, expiry and author.

## Performance

Measured on the shared development machine (local PostgreSQL, synthetic
data), before (migrations up to 0032) and after (0033 applied). The ingest
numbers do not read through the gate; their spread is machine noise.

| Measurement | Before | After |
|---|---|---|
| `test_idn_2_msg_1_throughput_20000`, messages/s (runs) | 12342, 11653 | 11343, 11441, 6517 (busy machine) |
| `test_idn_2_resolution_overhead`, ratio | 0.984, 1.091 | 0.971, 1.095, 1.078 |
| `test_emb_1_bulk_set_based_rebuild_100000`, s | 2.40, 2.36 | 2.59, 2.47, 3.34 (busy machine) |

Median change: throughput -5%, rebuild +9%; both inside the 20% bound.

`test_performance_unchanged` compares the gated view with its own ungated
definition on 30,000 messages:

| Statement | Ungated (`tl.message_versions_all`) | Gated (`api.messages`) |
|---|---|---|
| full count | 10.9 ms | 17.1 ms |
| keyword (`tsv @@ ...`, GIN index) | 3.9 ms | 3.5 ms |
| `api.search('quince', 10)` | - | 3.0 ms |
| full count as a restricted asker | - | 220 ms |

The unrestricted branch keeps its plan and index; a full scan pays for two
extra plan nodes. The restricted branch evaluates the gate once per scanned
row and cannot use caller predicates for index access.

## Sensitivity labels and disclosure (0051)

Privacy does not rest on model judgement. A work or friend agent cannot even
retrieve what its grant does not allow; a model only phrases what the gate
already returned. The rule lives in `api.asker_sees` and `api.row_visible`
(same signatures as before), so every gated view, search function, recall
leg, result set, packet, person function, coverage view and feed inherits
it without a change of its own.

### Labels

- `tl.labels(label, description)`: the label kinds. Seeded as a seed hook
  (`tl.seed_hooks` name `labels`, so `tl.reseed()` brings them back after a
  truncation): `intimate`, `medical`, `financial`, `work`,
  `third_party_confidence`. The owner may add more (`add_label_kind`); a name
  is lower case letters, digits and `_`, starting with a letter, at most 41
  characters.
- `tl.channel_labels(channel_id, label)`, `tl.thread_labels(thread_id,
  label)`: labels on a channel or a thread (deleted with it).
- `tl.messages.access_label` (set by plugins at ingest) counts as a label
  when its value names a `tl.labels` row. History is covered without a
  re-import: adding the kind `x` labels every stored message whose
  access_label is `x`, at once.
- A label kind is never renamed, and is not deleted while a stored message's
  access_label names it (a trigger refuses both, and the foreign keys refuse
  deleting a kind a channel or thread carries). Either would turn labelled
  history into unlabelled history and open it to every channel grant, so it
  fails closed: add the new kind and relabel the rows instead.

The owner connection writes them; the app role reads them; the reader reads
none of them.

### The rule (default deny)

Unrestricted askers are unchanged: they read everything. For a restricted
asker a row of channel C, thread T and message label M (when M names a label
kind) is visible when some grant of the asker

1. is on C or on T,
2. has not expired (`expires_at` NULL or later than `now()`), and
3. allows (`labels`) every label of C, every label of T and M.

Labelled content is therefore denied by default even inside a granted
channel: a grant without labels admits only unlabelled rows. When a channel
grant and a thread grant both match, the most permissive one decides (the
test is an EXISTS over grants). A thread grant does not escape the labels of
its channel. A page revision or derived record is visible only when every
source of its lineage passes the same rule, so a page citing a labelled
message stays hidden unless that label is allowed as well (FLT-3: labels
apply to stored history and to everything derived from it). `api.channels`
lists a channel only for an unexpired grant that admits it (a channel
labelled `work`, granted without `work`, is not listed). `api.link_types`
lists the active link-type names (vocabulary, no content) only to an asker
holding at least one unexpired grant; an asker whose grants have all
expired sees none, like an asker with no grant.

`tl.grant_allows(asker, channel_id, thread_id, message_label)` holds the
rule; it is owner-only and called by the SECURITY DEFINER gate functions.
`api.row_visible` writes it out because it runs once per scanned row.

Conversations mixing visible and hidden messages. A conversation chunk is
built from the text of every current message in its thread range, so its
`token_estimate`, `text_hash` and embedding also describe a message whose
own `access_label` is denied (or a page whose source is hidden). For a
restricted asker a chunk counts only when every current message in its
range passes `api.row_visible`: `api.conversation_visible(thread_id,
first_no, last_no)` holds that test, the `conversations` gate applies it,
and the meaning legs (`api.vector_records`, `api.search_meaning`, and so
`api.search_fused`) rank only chunks that pass it. A visible neighbour
therefore never ranks for words that only the hidden message holds; it is
still found by the lexical legs. Unrestricted askers are unaffected, and a
grant that allows the label admits the chunk again
(`test_p9_mixed_conversation_not_listed_or_ranked_for_restricted_asker`).

### Grants with purpose and expiry

`tl.asker_grants` gains `labels text[]` (the labels this grant permits; each
must name a label kind, enforced by a trigger), `purpose`, `expires_at` and
`created_by`. Granting a target that is already held replaces its labels,
purpose and expiry; an identical grant writes nothing. `extend` moves only
the expiry; on the command line `--expires` is required (`never` removes the
expiry), so a forgotten option cannot make an expiring grant permanent.

Purpose is audit, not policy: the gate never consults it. Every label and
grant change (`label`, `unlabel`, `label_kind`, `grant`, `regrant`, `extend`,
`revoke`, and `asker` for registering an asker, changing its mode or removing
it with the number of grants it held) is appended to `tl.access_events(id, op, asker, detail, actor, at)`
in the same transaction, with the target, labels, expiry and purpose in
`detail`. The table is append-only (a trigger refuses UPDATE and DELETE); the
app role may read it.

### Expiry and the monotonic version

An expiry is not a write, yet it changes what an asker reads, so it must move
the policy version like a revocation. The version is computed:

```
api.policy_version() = tl.policy_state.version
                       + count(grants with expires_at <= now())
```

When a grant's expiry passes, the second term rises by one, and every saved
result set, session, trace and investigation made before is refused (409)
exactly as after a revocation.

A grant change could lower the second term (deleting an expired grant, or
extending it into the future), so the triggers on `tl.asker_grants`
(`tl.bump_policy_grants`, replacing the generic statement bump) add

```
1 + (number of OLD rows already expired at the change)
```

to `tl.policy_state.version` per statement. Over k expired OLD rows the
stored version rises by 1 + k and the expired count falls by at most k, so
the sum rises by at least 1. A `TRUNCATE` of `tl.asker_grants` has no
transition table, so a BEFORE TRUNCATE trigger adds the number of expired
rows first and the 0033 AFTER TRUNCATE bump adds the one. Inserting a grant, or updating one to an expiry
in the past, raises both terms. The trigger judges expiry with
`clock_timestamp()`, so a grant already counted by any reader whose
transaction began after its expiry is compensated. An unchanged `grant` or
`extend` writes nothing and leaves the version alone. Adding a label kind
moves the version only when a stored message names it, so re-seeding an empty
database leaves the version at 1.

### What an asker can learn about its own access

`api.describe_access()` keeps its 0033 keys (`granted_channels` and
`granted_threads` now count unexpired grants only) and adds `grants`: for
each of the asker's own grants the channel name or thread id,
`target_labels` (the labels on that channel, or on that thread together with
its channel's, since a thread grant must cover both), `allowed_labels`,
`purpose`, `expires_at` and `expired`. It never names another asker, an
ungranted channel, the labels of a thread the asker was not granted, or a
total. Catalogue section `disclosure` (`api.describe('disclosure')`) states
the rule.

### Performance of the per-row barrier

The restricted branch evaluates the rule once per scanned row. Measured on
the shared development machine (local PostgreSQL, synthetic data), median of
five `SELECT count(*) FROM api.messages` as a restricted asker over 30,000
messages in 30 threads of one granted channel, every tenth message with
access_label `medical`:

| Body of `api.row_visible` | Rows visible | Time |
|---|---|---|
| 0036 (grant only) | 30,000 | 248 ms |
| 0051, no channel or thread labels | 27,000 | 515 ms |
| 0051, three threads labelled `intimate` | 24,300 | 521 ms |
| 0051, grant allows `intimate` and `medical` | 30,000 | 532 ms |

The label rule costs about 9 microseconds per scanned row: one lookup that
reads the channel's record kind and the message's access_label together, and
index probes of the channel and thread labels. Unrestricted askers do not pay
it (their branch has no barrier).

## Left for M6

- People and hidden-field policies, asker groups; per-message conversation
  visibility under such policies.
- Default-deny for unregistered askers named by an owner token (today
  `THROUGHLINE_UNKNOWN_ASKER`, default `unrestricted`).
- Timing side channels: the restricted branch's run time still grows with the
  number of hidden rows scanned.
- Per-row cost of the barrier branch at scale (a grant-aware index path); the
  label rule roughly doubles it (see the 0051 measurements).
- The 200-query red-team suite; SSO.
