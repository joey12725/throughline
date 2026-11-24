# Write contract

Module `throughline.writes`, migration `0048_write_contract.sql`, routes in
`throughline.api` and `api/routes/{claims,lineage,annotations,link_types}.py`,
SDK `throughline_sdk.Client` and `Plugin.execute`. Design priorities P5, P8,
MSG-2, LNK-1, PLG-4, BRN-3, ARC-5, ACL-6; scenarios write_replay and
link_vocabulary (per-item refusal).

The goal: every rule of the 100 KB page-mutation skill
becomes a constraint or a receipt, so no agent needs to read back what it
wrote.

## Rules of the old skill and what replaces them

| Rule an agent had to remember | Constraint or receipt now |
| --- | --- |
| Write acknowledgements are unverified: read everything back after `put_page` and `add_link`. | Every record write answers with a **receipt** read back from the stored row in the same transaction: `kind, id, record_id, version, status, stored_hash, counts {links_from, links_to, provenance_citing, passages, timeline, sources}, written_at, replayed`. |
| A retried write can apply twice; keep a log of what was sent. | **Idempotency keys**: `Idempotency-Key` (8-200 characters), scoped to the token, 30 days in `tl.write_receipts`. Same key and same request: the stored response, `replayed: true`, nothing else written. Same key, different request: 409. Two concurrent requests with one key serialize on an advisory lock: one writes, the others replay. |
| Hold a writer lock; check that nobody changed the page since you read it. | **Compare-and-set**: `expected_version` on ingest items (0 = must not exist; mismatch is a per-item `version_conflict` with `current_version`); `expected_hash` (a receipt's `stored_hash`) on link PATCH and supersede, claim supersede, retract and contradict, annotation supersede and retract, entity merge and split (409 with `current_hash`). |
| An additive write can duplicate a row; search before adding. | Natural keys: messages by `(channel, external_id)` with a payload hash (MSG-2); claims by natural key (a second source corroborates; the claim receipt says `corroborated`, `new_sources`, `superseded`); an equal active link is returned as `existing`. |
| A remote write can skip link and timeline extraction; count them afterwards. | Receipt `counts`: links from and to the record, provenance rows citing it, passages of a page revision, derived records linked `about` to it (timeline), claim sources. |
| Link types drift; use the right spelling. | The closed registry (0042) folds synonyms and holds plugin candidates; batches report `folded`, `candidate` or `refused` per item instead of failing the whole batch. |
| A batch can fail half way; find out which items landed. | **Per-item results**: messages (`rejected`, `aliased`), `link_results` in ingest, `POST /v1/links/batch` and `POST /v1/entities/batch` with `{index, status, reason, id, receipt}`. Authorization stays whole-batch: any failure is 403 with nothing written and no per-item existence detail. |
| A provisional (fingerprint) id can be re-imported under its native id as a duplicate. | **Message aliases** (`tl.message_aliases`) and **per-key deliveries** (`tl.message_deliveries`): see below. |
| Replacing a page must never clobber the old text. | A changed page is a new immutable version (have); receipts report the version. |
| Do not paste record text into logs. | Stored responses keep receipts, ids, counts and statuses only (`writes.skeleton`); a test checks that no record text reaches `tl.write_receipts`. |

## Stored hash

`tl.stored_hash(kind, id)` (sha256 hex over a canonical jsonb array of the
stored row; NULL when the row does not exist):

- message: channel, external_id, version, payload_hash, sha256 of content
- link: type, ends, method, evidence, confidence, validity, status, superseded_by
- entity: kind, name, external_ref, metadata, is_placeholder, merged_into
- claim: statement, type, attribution, sorted (source message, version,
  relation), status, superseded_by
- annotation: target, attribute, value, method, validity, status, superseded_by
- proposal: kind, sha256 of the payload, status
- person, identifier: the identity columns an assertion changes
- provenance (`provenance` for a derived message, `provenance:<kind>`
  otherwise): the derived record and its sorted sources

The app role may execute it; the reader may not.

## Message aliases (P8)

A message item may carry `aliases` (at most 10): earlier or provisional
external ids of the same record in the same channel. They are outside the
payload hash. Resolution order per item:

1. the external id is a known alias -> the canonical record;
2. a current record exists under the external id -> that record;
3. an alias names an existing record -> the delivered id becomes an alias of
   it (status `aliased`);
4. the external id and an alias are two existing records -> one `same_as`
   link (method `source`, status `pair_linked`); recall's dedup stage
   collapses the pair.

The first external id stays canonical (archive ids never change). Aliases of
a new record are kept for later deliveries. `{channel, external_id}` link
ends and provenance keys resolve aliases.

Versioning of an aliased record is per delivered key: a key already seen
creates a version only when its own payload changed since its last delivery;
a new key records its hash and creates a version only when the payload
differs and `fields.identity_quality` ranks strictly higher
(`THROUGHLINE_IDENTITY_QUALITY_ORDER`, default `native_id, beeper_id,
fingerprint, unknown`). Alternating archive and live runs create no versions;
a real edit under either key creates one. Items of one batch that share a
key, an alias or a canonical record are applied in order across passes.
Deliveries and aliases are serialized by the channel's ingest lock.

`GET /v1/describe` lists the sections `message_aliases` (feature
`message_aliases`) and `write_contract`.

## Pinned interface for route authors

```python
try:
    from .. import writes
except ImportError:  # before wave 5
    writes = None

@router.post("/v1/things")
def thing_create(data: dict, token: Token, request: Request):
    require_unrestricted(token)
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                return slot.response          # stored skeleton, replayed: true
            row = ...                         # the work
            writes.check_hash(conn, "thing", row_id, expected)  # optional CAS
            return slot.store(200, dict(row, receipt=writes.receipt(conn, "thing", row["id"])))
```

- `writes.header_key(request) -> str | None`: the only dependency-level
  helper; a key outside 8-200 characters is 400.
- `writes.idempotent(conn, token, request, body)`: call first inside the
  route's own connection and transaction. It takes an advisory transaction
  lock on (token, key) and looks the key up. `slot.replayed`,
  `slot.response`, `slot.status_code`; `slot.store(status_code, response)`
  before commit (a no-op without a key) returns the response unchanged.
  `THROUGHLINE_REQUIRE_IDEMPOTENCY=true` makes the key mandatory for agent
  tokens (400).
- `writes.receipt(conn, kind, id, extra=None) -> dict` for kinds message,
  link, entity, claim, annotation, proposal, provenance (id = derived record,
  extra `derived_kind`), person, identifier; LookupError when the row is
  missing.
- `writes.register_receipt(kind, fn)`: `fn(conn, id)` returns any of
  `version, status, stored_hash, counts, record_id, written_at`, or None when
  the row does not exist (for example commitments).
- `writes.row_receipt(kind, ident, row)`: a receipt for registry rows without
  a stored-hash kind (link types, identity registry imports).
- `writes.expected_hash(body)` pops and validates `expected_hash`;
  `writes.check_hash(conn, kind, id, expected)` raises 409 with the current
  hash.

Responses stored under a key are skeletons: dicts and lists, numbers,
booleans and nulls, and strings only under `kind, status, reason, code,
record_id, stored_hash, written_at, derived_kind, op, outcome`. Make sure a
replay is useful from those fields (ids and receipts).

## Purge

Purge hooks (ord 15-17) delete, for the purged channel, receipts whose
response names its messages, their links or the claims citing them, and the
channel's aliases and deliveries (both also cascade with the channel).

## SDK

`Client` sends `Idempotency-Key: tl-<sha256(plugin, route, delivery id,
canonical JSON)>` on link, link batch, provenance and coverage posts. The
delivery id is drawn fresh for every delivery and kept across its retries, so
a retry of a request the core already committed is a no-op replay, while a
later delivery of the same record (the next run) is processed again: the
core keeps existing rows, and a link end or cited record imported since then
now resolves (BRN-6). A content-only key would replay the first answer
forever. `Client.link_batch(links)` posts at most 1,000 links per request
(falling back to single posts on an older core). `Plugin.execute` delivers
consecutive link records through the batch route, counts results in the run
report under `links` (`records, created, existing, folded, candidate,
refused, error`) instead of failing, and raises on 401 or 403. The message
key `aliases` passes through unchanged.
