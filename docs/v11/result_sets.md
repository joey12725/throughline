# Result sets

Module `throughline.resultsets`; routes `throughline.api.routes.sets`;
commands `throughline lib set ...` and `throughline lib session show`;
migration `0039_result_sets.sql`; allow-list `allowlist/0039_result_sets.json`.

A result set is a named list of record ids that an agent works with across
calls: recall saves one, transforms derive new ones from old ones, match and
plans read and write them, and SQL reads them with `api.result_set(handle)`.
Requirements: QRY-34, QRY-35, ACL-9, ACL-10, QRY-30 (grouping, manifests),
LIB-9 (working sets), LIB-5 (inspect), CST-6.

## What a set is (and is not)

Result sets are **temporary working state, not derived data**. A set stores
ids and short operator output only:

| Table | Holds |
|---|---|
| `tl.sessions` | id, token, asker, policy version, a note (at most 500 characters), unresolved needs, lifetime |
| `tl.result_sets` | handle (`rs_` + 32 hex digits), session, token, asker, policy version, origin `{op, params, parents}`, completeness, counts, `as_of`, `known_by`, lifetime |
| `tl.result_set_members` | ordinal, prefix, ref_id, record_id, pinned message id and version, span, score, role, depends_on, discovery, meta |
| `tl.result_set_groups` | (handle, ordinal, grp, certain): a member may be in several groups, each certainly or tentatively |

No column holds record text. Every string in `meta`, `discovery`, `origin` and
the session's `unresolved` (and every
object key) is at most 500 characters, enforced by a CHECK in the database and
by `create()`; `meta` holds expansion refs, verdicts and other operator output.
Long discovery details and expansion values are kept as a sha256 only.

Content and visibility come from the gated record views registered in
`tl.record_sources` (`m` messages, `p` passages, `l` links, `e` entities, later
`c` claims), joined live on every read:

- a member whose record the asker cannot see now simply does not join, so it
  is absent from `members`, `read`, `info().member_count`, `counts.returned`,
  every inspect view and SQL alike (ACL-9). That covers page members whose
  source became hidden, entity members (unrestricted-only) and link members
  whose evidence names hidden messages, because those views decide it;
- a message member reads its **pinned version** with that version's text,
  even after the message is edited (`is_current` is then false);
- a prefix with no registered view yields no rows.

## Ownership and lifetime (ACL-10, CST-6)

A set or session is usable only when all of these hold:

- it belongs to the same token and the same asker (an absent asker is NULL)
  as the request;
- the token is not revoked (deleting the token deletes its sets);
- the access policy version (`api.policy_version()`) is the one it was
  written under: any change of askers, grants or token askers invalidates
  every existing set. Purging a channel deletes its threads and their grants,
  so it also invalidates earlier sets;
- it has not expired.

Otherwise `api.result_set` and `api.result_set_info` return no rows. The
Python functions raise `ResultSetNotFound` for an unknown, foreign, expired or
revoked set (indistinguishable) and `ResultSetStale` for a set from an earlier
policy. Over HTTP these are 404 and 409. A stale set can still be dropped.

Lifetime: `THROUGHLINE_RESULT_SET_TTL_S` (default 86400 = 24 hours), at most
7 days; `ttl_s` may shorten or lengthen it within that bound, and a set never
outlives its session. `resultsets.expire()` deletes expired sets and sessions;
every `open_session` and `create` runs it first.

Purge: members carry a foreign key to the pinned message with `ON DELETE
CASCADE`, so `throughline purge` removes them with the messages; members of
other kinds disappear because their record view no longer has the row. No
purge hook is needed because no content is stored. A purge also deletes the
channel's threads, which cascades into `tl.asker_grants`; that moves the policy
version, so every set created before the purge is stale afterwards (409) and
must be recreated. This is deliberate: a purge changes what every asker can
see, and stale sets fail closed rather than answer from the old policy.

Dump and restore: the tables are ordinary tables and travel with
`throughline dump` / `restore`. A restored set stays readable by the same token
and asker until it expires, as long as the policy version is unchanged.

## SQL

```sql
SELECT r.record_id, m.channel, r.score
FROM api.result_set('rs_...') r
JOIN api.messages m ON m.id = r.message_id
ORDER BY r.ordinal;

SELECT completeness, member_count, counts FROM api.result_set_info('rs_...');
```

`api.result_set(handle)` returns `ordinal`, the record contract columns
(`record_id ... expansion`, in contract order), then `span_first`,
`span_last`, `score`, `role`, `depends_on`, `groups` (`[{grp, certain}]`),
`discovery`, `meta` and `set_completeness`. Both functions are exact and free;
a `ranked_sample` set is not a population, so aggregates over it describe the
sample only. The reader cannot create tables: sets are written by the
application role and read through these two functions only.

## Python

Every function takes the `Scope` first.

- `open_session(scope, note=None, ttl_s=None)`; `session(scope, id)` returns
  the session's sets in creation order (origin, parents, completeness, counts)
  and its `unresolved` list; `set_unresolved(scope, id, items)`.
- `create(scope, records, *, origin=None, completeness, counts=None,
  session=None, as_of=None, known_by=None, ttl_s=None)` returns a handle.
  Records are dicts or objects with `record_id` and optionally `version`,
  `message_id`, `span {first_no, last_no}`, `score`, `role`, `depends_on`,
  `groups`, `discovery`, `meta`, `expansion` (kept in `meta.expansion`). A
  message record without a version is pinned to the version the scope sees.
- `resolve(scope, record_ids)` returns the visible rows of caller-named ids.
- `members(scope, handle)`: visible members without text.
- `info(scope, handle)`: completeness, counts, origin, as_of, lifetime,
  `member_count` of visible members.
- `read(scope, handle, limit=None, offset=0, token_budget=None)` returns
  `{records, info, next_offset, execution}`. Records come from the shared
  hydrator `retrieval.hydrate.records_for_members` (identical keys to recall),
  or from `api.result_set` rows with the same keys when it is absent.
- `transform(scope, op, handles, **params)`, `inspect(scope, handle, view,
  selector=None, token_budget=1000)`, `drop(scope, handle)`, `expire()`.

`counts` keeps `retrieved`, `mechanically_processed`, `semantically_evaluated`,
`inspected_by_controller` and `returned` apart; `returned` is always the number
of members visible now. A transform counts the members it read as retrieved
and mechanically processed.

## Transforms (QRY-35)

Each creates a new set with `origin = {op, params, parents}` in the session of
its first input (unless `session` is given). Inputs are read through the gate.

| op | parameters |
|---|---|
| `union`, `intersect`, `subtract` | two or more handles (union also one); identity is `record_id` + `version` |
| `filter` | `kinds`, `channels`, `after`, `before` (record time, half-open), `min_score`, `role`, `grp`, `meta` `{key: value}` |
| `order` | `by` score (default, descending) \| record_time \| ordinal \| record_id, `desc` |
| `head` | `n` |
| `sample` | `n`, `seed` (deterministic) |
| `dedup` | `by` record_id (default) \| span (overlapping spans in a thread) \| support (identical support sets) |
| `group` | `by` conversation \| thread \| channel \| day \| month \| year \| person \| support \| explicit (`assignments {record_id: [grp or {grp, certain}]}`), `basis` record \| subject, `replace` |
| `ungroup` | `prefix` (only groups starting with it), else all |

Grouping writes `tl.result_set_groups` on the new set; the parent is unchanged,
so grouping is reversible. Uncertain membership: a message not yet in a
conversation is grouped by its thread with `certain=false`; a member without a
person is in `person:unknown` with `certain=false`; with `basis=subject`, a
subject interval spanning several buckets or coarser than the bucket is
uncertain in each bucket.

Completeness: the worst input completeness is carried (`ranked_sample` is
worse than `truncated`, which is worse than `exhaustive`). An operation that
does not keep every member it is meant to keep lowers it: `head` and lossy
`dedup` (span, support) of an exhaustive set give `truncated`, `sample` gives
`ranked_sample`. `filter`, set algebra, `order`, `group` and record-id `dedup`
keep it.

## Inspect (LIB-5, QRY-30)

`inspect` returns `{view, handle, completeness, member_count, items,
items_total, truncated, ...}` estimated at most `token_budget` tokens (items
are cut first, then optional keys, listed in `omitted`):

- `summary`: counts, kinds, roles, time and score range, number of groups,
  items = channels with counts;
- `sample`: selector `{n, offset, grp}`, items with an excerpt of at most 200
  characters read live through the gate (returned, never stored);
- `by_time` (selector `granularity` day \| month \| year), `by_channel`,
  `by_person`;
- `groups`: the manifest (members, certain, uncertain, channels, first and
  last time per group, `ungrouped`, `multi_group_members`); with selector
  `{grp}` the members of one group;
- `missing_link`: records named by `depends_on` or by support that are not in
  the set;
- `origin`: the chain of parent sets with operation and parameters;
- `history`: the session's sets and unresolved needs.

## HTTP

Owner or agent tokens; the sets are the caller's own working state, so the
routes do not refuse a restricted asker (`deps.own_working_state`). Every
record id a caller supplies (including `depends_on`, origin parents and the
session) is checked through the gated views first; unknown, hidden and foreign
are the same 404.

| Route | |
|---|---|
| `POST /v1/sessions` | `{note, ttl_s}` |
| `GET /v1/sessions/{id}` | sets in creation order, unresolved |
| `POST /v1/sets` | `{records: [{record_id, version, span, score, role, depends_on, groups, meta}], session, origin, completeness (default exhaustive), counts, as_of, known_by, ttl_s}` returns info |
| `GET /v1/sets/{handle}` | `?limit&offset&token_budget` (capped by the token's max_return_tokens) |
| `GET /v1/sets/{handle}/info` | |
| `POST /v1/sets/transform` | `{op, handles, params, session, ttl_s}` returns info |
| `POST /v1/sets/{handle}/inspect` | `{view, selector, token_budget}` |
| `DELETE /v1/sets/{handle}` | |

CLI (over HTTP with `THROUGHLINE_TOKEN`): `throughline lib set show|info|
inspect|transform|drop` and `throughline lib session show`.
