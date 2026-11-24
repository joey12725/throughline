# Link types

Module `throughline.vocabulary`, routes `throughline.api.routes.link_types`,
trigger `tl.link_type_gate` (migration `0042_claims.sql`), CLI `throughline
lib link-type list|register|merge|retire`. Requirements LNK-1, LNK-2, IDN-6,
scenario link_vocabulary; design priority P5.

**Free-text link types are refused or folded.** Every link type lives in the
closed registry `tl.link_types` (`type`, `description`, `inverse`, `status`
active | candidate | retired, `origin` core | plugin | owner | legacy |
source_candidate, `plugin`, `first_seen`, `last_written_while_retired`), with
synonyms in `tl.link_type_synonyms` (alias -> type).

## Normalisation (total)

`tl.normalize_link_type(raw)` always returns a valid name
(`^[a-z][a-z0-9_]{0,62}$`): lower case, trimmed, every run of other
characters becomes `_`, edge underscores stripped, cut to 63 characters. A
name that does not start with a letter gets `t_`; nothing left (only symbols
or non-ASCII) becomes `t_` plus 12 hex digits of sha256 of the raw text. When
normalisation or folding changes a type, the raw text is kept in
`tl.links.type_as_written`.

## The gate (every writer, method first)

On INSERT or UPDATE OF type on `tl.links`:

1. A synonym is folded into its canonical type; an active type is kept.
2. Method `rule` or `model` on an unregistered type is refused, whatever the
   connection – the librarian's curate mode or a media-noise run on the owner database
   connection cannot invent types.
3. Method `owner`, or an owner token: the type is registered (active, origin
   owner).
4. A token-less connection that is not a runtime role (migrations, owner
   registry imports) writing `owner` or `source`: registered, origin owner.
5. A plugin token writing `source`:
   - declared in its manifest `link_types`: registered active, origin plugin
     (manifest `link_types` is the plugin's declared vocabulary);
   - undeclared: accepted and held as a `candidate` (origin
     source_candidate, `first_seen`) for the owner to register or merge;
   - retired: folded into its merge target when one exists, else accepted
     and listed as `retired_but_written`.
   Imports are never stopped by the vocabulary.
6. Everyone else (agent tokens, the app role without a token, a retired type
   from a non-owner, non-plugin writer) is refused: `unregistered link type:
   <t>; register it or propose it` (SQLSTATE 23514, HTTP 400) with a hint.

`ingest.add_link` resolves the type (`tl.resolve_link_type`, else the
normalised name) before its replay lookup and lock, so replaying a link under
a synonym returns the existing folded link.

## Reading

- `api.link_types` (`type, description, inverse, status, origin, synonyms`):
  unrestricted askers see every type; a restricted asker with any grant sees
  active types only; no counts (they would count hidden links).
- `GET /v1/link-types` reads that view. `?with_counts=true` and
  `?status=candidate|retired` are owner-side (restricted: 403) and add
  `active_links`, `links`, `source_links`, `retired_but_written`.

## Changing the registry (owner)

- `POST /v1/link-types {type, description, inverse}` – register (a candidate
  or retired type becomes active; a synonym alias cannot be registered).
- `POST /v1/link-types/{type}/merge {into}` – one transaction: creates the
  synonym (and repoints synonyms of `type`), re-types every link (history is
  kept by the link history trigger, `type_as_written` keeps the original),
  then collapses duplicates: in every group of active links with the same
  type, ends and method that a re-typed link joined, all but the oldest are
  superseded by the oldest. The type is retired (a candidate is removed).
  Returns `{type, into, retyped, collapsed}`. Future writes of the old name
  fold into the target.
- `POST /v1/link-types/{type}/retire {into?}` – a type imports depend on
  (origin plugin, legacy or source_candidate, or with an active source link)
  needs `into`, and retiring becomes a merge. Other types retire plainly;
  owner writes of a retired type are accepted and recorded.

Others **propose and the owner merges**: `POST /v1/proposals` with kind
`link_type` and payload `{type, description}` (register) or `{type,
merge_into}` (merge); acceptance runs the same functions.

## Seeds and existing data

Seeds are stored as seed hooks (`tl.seed_hooks`, run by `tl.reseed()`): the
types the core, the shipped plugins and the tests use, as active core types,
and core synonyms (replies_to, in_reply_to -> reply_to; partof, is_part_of ->
part_of; ...). Test truncation re-runs them, so registrations made by one test
never leak into the next. Migration 0042 itself normalised existing link
types, folded obvious variants and registered every remaining type as active
`legacy`, so existing data stays valid.
