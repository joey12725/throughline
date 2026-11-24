# Images in Throughline

Images are a native modality, not a separate gallery. Every image, whether it came
from a chat, an email, a Hermes or T3 Code conversation, or the Immich camera
roll, is one content-addressed **asset**. Its bytes live in Immich, and it is
linked to the messages it belongs to. It also carries the people it depicts, its
place and time, its tags and its OCR text. Agents meet images inside the same
find, timeline, context and orient results as text, as an `images[]` array on
each message.

## Deviations

Additive or clarifying changes made while building the core half (schema
names and columns are as specified below):

- `render_text` prints a `caption: ...` line when the asset has a caption,
  and an unclassified asset's header starts with `image`.
- `api.assets_of(message_ids bigint[], include_nsfw boolean DEFAULT false)`:
  the second argument is optional, so the one-argument call works.
- `images[]` items have `thumb_url` and `content_url` `null` while the asset
  is not in Immich yet. In `/v1/recall` records the key `images` is always
  present (`null` without images), because every record has the same keys;
  in find, timeline and orient a message without images has no key.
- The Immich person name of a face identifier is kept in
  `tl.identifiers.label` (identifiers have no display name). A name that is
  exactly one active person's display name links the identifier to that
  person with `status='suspected'`; only `confirmed` identifiers name faces,
  write depicts links and enter the search text.
- Signed URLs for `<img src>` are not built: the web client fetches thumb and
  content with its bearer header and shows a `blob:` URL (or uses
  `POST /v1/assets/inline`). No route is reachable without the bearer token.
- The keyword search over image text is a leg of its own, `image_lexical`,
  next to `visual`.

## Fixes after review (migration 0064)

`migrations/0064_images_fixes.sql` (0063 is applied live and never edited):

- **Face recognition is unrestricted-only.** The `links` gate leaves out
  `type='depicts'` for restricted askers (`AND (v.type<>'depicts' OR
  api.asker_unrestricted())`, otherwise the 0033 predicate), and so does
  `api.link_image_visible` (link history, `links_asof`, provenance).
  `tl.recall_scope`'s involved clause reads the gated `api.links`, and
  timeline's own depicts clause is added only when the asker is
  unrestricted. A restricted asker's `person_role=involved` never reaches a
  photo through a face.
- **Face identifiers start suspected.** `store.replace_faces` inserts a new
  Immich face cluster with `status='suspected'` (the table default is
  confirmed); 0064 turned the existing confirmed clusters without a person
  into suspected ones (ignored ones stay ignored).
- **Identity changes refresh depicts (triggers).** An `AFTER INSERT / UPDATE
  OF person_id,status / DELETE` trigger on `tl.identifiers` (Immich face rows
  that are, or were, confirmed with a person) calls
  `tl.refresh_asset_depicts(asset_ids)` for the assets with faces of that
  identifier: it touches the assets (search text, names) and rewrites their
  messages' depicts links. `identity.attach`, `merge`, `split` and
  `POST /v1/identity/identifiers/{id}/status` all write `tl.identifiers`, so
  no caller has to remember `store.refresh_depicts`. A trigger on
  `tl.people` (`display_name`, `merged_into`) refreshes the assets showing
  that person. `tl.refresh_asset_depicts` is the SQL twin of
  `store.refresh_depicts(conn, asset_id)`; keep the two in line.
- **Unscored is unsafe.** `tl.asset_safe(nsfw, kind, enrich)`: scored below
  0.6 is safe; scored at or above 0.6 is not; with no score, a video is safe
  (NudeNet is image-only, so a video's `nsfw` stays null) and an image is
  safe only once its nsfw stage completed without an error. Every nsfw
  filter (`assets_of`, `asset_pick`, `visual_assets`, `image_search_assets`,
  `/v1/assets/search`, inline, the people routes) uses it plus the tag
  `nsfw`. `api.assets` gains the column `safe`; it still lists every visible
  asset, safe or not (owner tooling: filter on `safe`).
- **NudeNet errors are retried.** An error is not "done": the pipeline records
  `enrich.nsfw_error = {error, attempts, at}` and tries again next run; after 3
  attempts it gives up with `enrich.nsfw = {error, gave_up: true, attempts}`
  and `nsfw` null. A given-up asset stays unsafe. An `enrich.nsfw.error` left
  by the older code is retried too.
- **Direct asset routes.** `GET /v1/assets/{id}`, `/thumb` and `/content`
  answer 404 for an asset that is not safe unless `?include_nsfw=true`.
- **`photos.immich` text names nobody.** Its content is `render_text` without
  people (a message is gated by channel only; names are face recognition).
  An unsafe asset's content is `[photo · <place> · <time>]` only, and its
  fields carry kind `photo` and no tags.
- **Immich descriptions carry no message text**: source, thread title and
  author only (`Discord · #general · from Alex Kim`), version
  `enrich.describe_v = 2`. `throughline lib images redescribe [--limit N]`
  rewrites the descriptions of assets uploaded before (idempotent; run it
  until `redescribed` is 0).
- **Pipeline.** An Immich 404 on pull clears `immich_id`, sets `pending` and
  drops `enrich.pull`, so the asset uploads again (from the spool, or after
  the next backfill refetches it once upload marks it missing); an older
  `missing` row with an `immich_id` is reset the same way when its bytes are
  fetched again. Fetched bytes that do not sniff as an image are not stored
  (`skipped:not_image_bytes`). `beeper_fetch.py` reports `name_mismatch`
  when the attachment at the index has another file name and the name
  matches no attachment or several.
- **`image_search_records`** ranks every keyword hit when the 5000 best hits
  leave fewer than `max_results` messages after the hard scope.

## Settings, shared posts and location (migration 0065)

`migrations/0065_image_settings.sql`. The code runs before and after it: it
detects `tl.image_settings` (`to_regclass`) and otherwise keeps the 0064
behaviour (hide, threshold 0.6, no previews role, no locate stage).

### Image settings

One row, `tl.image_settings` (seed hook `image_settings`). Read and change
it with `GET` / `PUT /v1/images/settings` (owner tokens only, unrestricted;
PUT takes the fields to change, honours `Idempotency-Key` and answers a
receipt of kind `image_settings`; 400 for an unknown field or a bad value,
409 before 0065) or `throughline lib images settings [--set key=value ...]`.

| Setting | Values (default) | Meaning |
| --- | --- | --- |
| `nsfw_mode` | `hide` \| `blur` \| `show` (`hide`) | Unsafe images for unrestricted askers. **hide**: left out of search, timeline, images[] and inline, and the direct routes answer 404, unless `include_nsfw=true`. **blur**: listed with `nsfw: true`; thumb, content and inline serve a server-side Gaussian blur (radius 8% of the long side, JPEG) unless `include_nsfw=true`. **show**: no filtering; the `nsfw` flag and tag stay. |
| `restricted_nsfw_mode` | same (`hide`) | The mode for restricted askers (asker scope). |
| `nsfw_threshold` | `0 < x <= 1` (`0.6`) | A scored image is unsafe at or above it. |
| `unscanned` | `hide` \| `show` (`hide`) | Images not scored yet (or whose scoring failed or gave up): unsafe (`hide`) or safe (`show`). Videos are safe either way. |
| `shared_previews` | `include` \| `exclude` (`include`) | Shared-post covers in images[] and image search. |
| `location_fallback` | `off` \| `throughline` \| `throughline_and_immich` (`throughline_and_immich`) | Location from history (below). |
| `location_window_min` | 1-1440 (`30`) | ± minutes around `taken_at` for a track point. |
| `location_max_accuracy_m` | 1-100000 (`500`) | Track points less accurate than this (or without an accuracy) are not used. |

Decisions and details:

- **Safe.** `tl.asset_clean(nsfw, kind, enrich, tags, threshold, unscanned)`:
  a scored asset is judged by its score only (the tag no longer overrides
  it); an unscored one is unsafe when tagged `nsfw`, else safe when it is a
  video, when `unscanned='show'`, or once its nsfw stage completed.
  `tl.asset_safe(nsfw, kind, enrich)` now reads the threshold and
  `unscanned` from the settings (STABLE); the pure 5-argument form takes them
  as arguments. `tl.image_policy()` gives the settings for the current asker
  (`nsfw_mode` is `restricted_nsfw_mode` for a restricted asker); every
  filter reads it once per statement. Bodies are SQL-standard (`RETURN`), so
  the reader role needs no USAGE on schema `tl`.
- **Threshold changes.** Filtering follows a new threshold at once (it uses
  the score). The `nsfw` tag is set at classification time with the
  threshold of that moment; after a change the tag catches up at the next
  tags stage (`throughline lib images enrich --stage tags`, or the timer's
  sync), which adds or removes `nsfw` on every scored asset, in Immich too.
- **Every filter follows the asker's mode**: `api.assets_of`, `tl.asset_pick`
  (visual and image-keyword legs), `api.visual_assets`,
  `api.image_search_assets`, `api.assets.safe` (asset routes, search,
  inline) and the people routes (unrestricted, so `nsfw_mode`).
- **`include_nsfw` for restricted askers** is honoured as for the owner:
  `restricted_nsfw_mode` sets what a restricted asker gets by default, the
  same way `nsfw_mode` does for the owner.
- images[] items gain `nsfw` (bool) and `location_source` (`exif`, `track` or
  null). A flagged image that is not blurred (hide with `include_nsfw`, or
  show) gets `thumb_url`/`content_url` with `?include_nsfw=true`.
  `api.assets_of` returns `nsfw`, `blurred` and `location_source`.
  `GET /v1/assets/{id}`: `nsfw` is now the flag and `nsfw_score` the score;
  inline items served blurred carry `blurred: true`.

### Shared-post previews

Many images shared in social-media messaging threads are the cover of a shared
reel or post rather than photos anyone in the conversation took.

- Detection (`images/shared.py`, any Beeper network): the message's text,
  without the import's `reactions:` lines, is one link to a post, or the
  post's author and caption followed by the link on its own last line
  (Beeper's Instagram form), with no other link. Links:
  `instagram.com/[user/](p|reel|reels|tv)/`, `tiktok.com/@user/video/`,
  `vm.`/`vt.tiktok.com`, `youtube.com/shorts/`, `x.com|twitter.com/user/status/`.
  Stories, Threads and Facebook shares are counted by the dry run, not marked.
- Marking: `message_assets.role = 'preview'`, `source_ref.shared_url` (and
  `shared_network`), asset tag `shared-post` (kept by the tags stage), Immich
  tag `shared-post` and album `Shared posts`. Before 0065 a new preview link
  is stored as `attachment` with its `shared_url` and tag;
  `reclassify-previews` fixes the role.
- People graph: preview links write no depicts links (`store._refresh` and
  `tl.refresh_asset_depicts` skip them); assets whose every link is a preview
  are left out of photographed-with and nearby; Immich clusters whose every
  face is on such assets are not offered as `people sync` suggestions
  (`skipped_preview_only`). The faces rows stay.
- `shared_previews=exclude` leaves preview links out of images[]
  (`assets_of`), the image legs (`asset_pick`) and asset search
  (`visual_assets`, `image_search_assets`, `api.assets.preview`). The direct
  asset routes still serve them.
- Existing assets: `throughline lib images reclassify-previews [--dry-run]`
  (idempotent; the dry run is read-only and works before 0065). The first
  sync after 0065 runs it by itself, before locate, and records
  `<IMAGES_STATE_DIR>/reclassify_previews.json`; until that file exists sync
  skips locate (`waiting_for_reclassify_previews`), so no cover is placed or
  pushed to Immich first.

### Location from history

Stage `locate` (sync runs it after pull; `enrich --stage locate`):

- An asset without EXIF GPS (`lat` null, or `loc_method='track'`), pulled,
  with a link that is not a preview, takes the nearest non-withdrawn
  `tl.location_track` point within ±`location_window_min` of `taken_at` with
  `accuracy_m <= location_max_accuracy_m` (closest in time, then most
  accurate). It sets `lat`, `lon`, `loc_method='track'`, `loc_accuracy_m`.
  EXIF always wins: pull sets `loc_method='exif'` (and keeps `track` when
  Immich only echoes the location we pushed).
- `throughline_and_immich` also writes `latitude`/`longitude` to the Immich
  asset with the description plus ` · location from history`. Immich
  reverse-geocodes it; a later run (after 2 minutes, for up to 6 hours) reads
  city/state/country back into `tl.assets.place`.
- `enrich.locate = {v, key, track_id_max, track_id, at, immich}`. An asset is
  looked at again only when a track point with a higher id than
  `track_id_max` falls in its window (track data that arrives after the
  photo, backfills included), the point that placed it was withdrawn, or the
  window or accuracy setting changed (`key`). One indexed lookup per asset.
- `render_text` marks the place: `[photo · near Austin, Texas, US (from
  location history)]`.

## Known limitations

- Location from history is the **owner's** location at `taken_at`. For an
  image someone else sent (most chat images have no EXIF time, so `taken_at`
  is the message time) that is where the owner was when it arrived, not where
  it was taken.
- `location_fallback='off'` stops new inferences but leaves existing track
  locations; a withdrawn point clears the location in Throughline but not in
  Immich (Immich keeps the coordinates written earlier).
- `redescribe` rewrites a description without the location suffix (only
  assets still on describe_v 1).
- `api.assets.preview` is computed over all links of the asset, not only
  the visible ones (one bit about hidden links, like finding C).

- An asset shared by a hidden and a visible channel is one asset: its tags,
  caption, OCR and `origin` come from whichever source stored it first, so a
  restricted asker who sees it through the visible message also sees facts
  that came from the hidden one (finding C, not fixed).
- `_models_present` (the visual leg) checks on the app role, ungated, whether
  any linked image in the requested channels has a vector of a model. For a
  restricted asker this leaks one bit per model about hidden channels'
  images (whether the leg skips or embeds; finding C, not fixed).
- `api.assets` lists unsafe assets (with `safe = false`) for every visible
  message; it is owner tooling over `/v1/sql`, not a response surface.

## Design decisions

- **Storage.** Every image is stored in Immich, in the main timeline. Nothing is
  archived or hidden. Immich runs locally (`~/.throughline/immich`, port 2283,
  GPU machine learning) or on another host, so nothing may hard-code
  local paths: use `IMMICH_URL` and `IMMICH_API_KEY` (in `~/.throughline/immich.env`).
- **Not life events.** A photo is not assumed to be a life event. Many images are
  screenshots and practical things.
- **One `kind` per asset**, from this closed set:
  - `photo`: a camera photo of the world
  - `screenshot`: a screenshot of a screen, including UI captures
  - `document`: a receipt, scan, form, page, slide or ticket (receipts are documents)
  - `artwork`: a drawing, illustration, sticker, emoji art, logo or generated art
    (stickers are artwork)
  - `video`: a video
  - `other`
- **Tags.** `tags` is a free list of strong, normalised tags, and `meme` is one of
  them. There is no `is_meme` column.
- **Filters.** Tags and filters cover kind, tags, person, place, source, time and nsfw.
- **OCR** runs only on images that contain text. Immich's own OCR (PP-OCRv5) is
  the engine; its text is pulled into `ocr_text`. Empty means no text.
- **NSFW.** NudeNet runs locally. It records `nsfw` (0 to 1) and the labels it
  found. An image at or above the threshold (setting `nsfw_threshold`,
  default 0.6) gets the `nsfw` tag. What happens to unsafe images is the
  owner's setting (`nsfw_mode`, `restricted_nsfw_mode`, `unscanned`; see
  "Settings, shared posts and location" above); the default hides them from
  search, timeline and inline image results unless the request passes
  `include_nsfw=true`, and hides images not scored yet the same way. Videos
  are not scored and count as safe.
- **Local CLIP.** Immich runs the SigLIP2 model `ViT-SO400M-16-SigLIP2-384__webli`.
  Its image vectors (read from Immich's own Postgres) are copied into
  `tl.asset_vectors` under model `siglip2-so400m-384` and used for:
  - zero-shot tags and kind (against text prompts embedded through the Immich ML
    `/predict` endpoint), and
  - a local text-to-image search leg.
- **Gemini multimodal embeddings.** Image vectors come from `gemini-embedding-2`
  at 1536 dims (key `GEMINI_API_KEY` in `~/.throughline/gemini.env`), stored in
  `tl.asset_vectors` under model `gemini-embedding-2`. These give meaning beyond
  keywords ("on the beach and ocean").
  - **Text is never re-embedded with Gemini**, because voyage-context-4 is better
    for text.
  - At query time, when images are eligible, the query is embedded twice: with
    Voyage for the text legs, and with Gemini for the visual leg. Query vectors
    are cached in `tl.semantic_cache`.
  - Embedding the library goes through the batch API and the budget ledger
    (`providers.call`). Embedding a query is cheap.
- **Names in results.** Results carry depicted people by name (`people:
  [{person_id, name}]`).
- **Email.** Only images from personal mailboxes are taken: senders at gmail,
  googlemail, aol, hotmail, outlook, live, msn, yahoo, icloud, me, mac, proton,
  protonmail or pm.me. The `@` domain is checked, and sender local parts like
  `noreply` are excluded. Images under 15 KB or under 100 px a side are skipped
  as signature or tracking images.
- Enrichment is optional and resumable. Backfill runs behind an explicit command
  with per-run limits.

## Schema (migration `0063_images.sql`)

```
tl.assets
  id            bigint identity PK
  sha256        text UNIQUE NOT NULL         -- of the original bytes
  immich_id     uuid UNIQUE                  -- null until uploaded
  mime          text, bytes bigint, width int, height int, duration_s real
  kind          text CHECK (kind IN ('photo','screenshot','document','artwork','video','other')) -- null = not classified yet
  tags          text[] NOT NULL DEFAULT '{}' -- lower-case, words joined by '-', e.g. 'meme','beach','receipt','code'
  ocr_text      text                         -- null = not run, '' = no text
  caption       text                         -- optional short description (may stay null)
  taken_at      timestamptz                  -- EXIF original time, else the earliest linked message time
  lat, lon      double precision             -- EXIF GPS, else the location track (0065), else null
  loc_method    text                         -- 'exif' | 'track' | null (0065)
  loc_accuracy_m real                        -- the track point's accuracy (0065)
  place         jsonb                        -- {city,state,country} from Immich reverse geocoding
  nsfw          real, nsfw_labels jsonb
  origin        text NOT NULL                -- first source: 'beeper','email','t3code','hermes','immich'
  original_name text
  status        text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','uploaded','enriched','failed','missing'))
  enrich        jsonb NOT NULL DEFAULT '{}'  -- per-stage versions/errors: {"faces":..., "ocr":..., "nsfw":..., "tags":..., "gemini":...}
  created_at, updated_at timestamptz
  tsv           tsvector  -- maintained by trigger from kind, tags, ocr_text, caption, place, original_name and the
                          -- depicted people's names; GIN index

tl.message_assets          -- keyed by the record, so it survives new message versions
  channel_id bigint NOT NULL REFERENCES tl.channels
  external_id text NOT NULL
  asset_id bigint NOT NULL REFERENCES tl.assets ON DELETE CASCADE
  ord int NOT NULL DEFAULT 0        -- position among the message's attachments
  role text NOT NULL DEFAULT 'attachment' CHECK (role IN ('attachment','inline','self','preview'))
                                    -- 'self' = the message IS the photo (photos.immich)
                                    -- 'preview' = the cover of a shared post (0065)
  source_ref jsonb NOT NULL DEFAULT '{}'  -- how it was fetched (url / attachment id / file name)
  PRIMARY KEY (channel_id, external_id, asset_id)

tl.asset_faces
  id bigint identity PK
  asset_id bigint NOT NULL REFERENCES tl.assets ON DELETE CASCADE
  immich_face_id uuid UNIQUE
  identifier_id bigint REFERENCES tl.identifiers   -- the Immich person as identifier system='immich', kind='face'
  bbox jsonb NOT NULL                                -- {x1,y1,x2,y2,w,h} in image pixels
  score real

tl.asset_vectors
  asset_id bigint REFERENCES tl.assets ON DELETE CASCADE, model text, embedding vector, created_at
  PRIMARY KEY (asset_id, model)
  -- partial HNSW per model on (embedding::halfvec(dims)) halfvec_cosine_ops
```

Identity:
- One Immich person (a face cluster) is a `tl.identifiers` row with
  `system='immich'`, `kind='face'` and `value=<immich person uuid>`. Seed the
  `tl.identifier_schemes` row for it.
- Linking that identifier to a person (`identity.attach`) names every face in the
  cluster. Naming a person in Throughline pushes the name to Immich, and naming
  them in Immich creates a suspected link by exact display name.
- A new link type `depicts` (message → person, inverse `depicted_in`) is written
  for every linked message with a confirmed face. This makes `person_role=involved`
  return photos of the person through the existing links clause. Timeline's own
  involved clause gets the same addition.

Gating: the gated views are `api.assets`, `api.message_assets` and
`api.asset_faces`.
- `api.message_assets` joins the current message version, exposes `message_id`,
  and is gated by `api.message_visible(message_id)`.
- `api.assets` is visible when the asset has at least one visible message link.
- `api.asset_faces` is unrestricted-only, like people.
- Purge: a purge hook deletes the `message_assets` rows of a purged channel and
  orphaned assets' rows. The Immich bytes stay; they are the owner's library.

Channel `photos.immich`: one message per asset that was uploaded to Immich
directly, by the phone app or the web, and not by the pipeline.
- Role is `self`, `happened_at` is `taken_at`, `lat`/`lon` come from EXIF, and
  author is the owner.
- The thread is a "photo session": consecutive assets less than 2 hours and 3 km apart.
- Content is the asset's text rendering without people names (see below;
  unsafe assets: place and time only). Images from other sources
  get no extra message; they stay attached to their original message.

## Text rendering

The same short text block is used for lexical search, for agents, and as the
`photos.immich` content:

```
[photo · beach, ocean, sunset · Galveston, Texas, US · with Alex Kim, Sam Lee]
text: "WELCOME TO GALVESTON"
```

Only the parts that exist are printed. OCR is cut to 300 characters inline, and
the full text is in `ocr_text`.

## API

- `images[]` on every message in `/v1/find`, `/v1/timeline`, `/v1/orient` and
  `/v1/recall` records:
  `{asset_id, kind, tags, people:[{person_id,name}], place, location_source, taken_at, ocr (<=300 chars), caption, width, height, nsfw, thumb_url, content_url}`.
  Unsafe assets follow the image settings (hidden unless `include_nsfw` by default). `api.assets_of(message_ids bigint[])`
  returns the gated rows in one call.
- `GET /v1/assets/{id}` returns metadata. `GET /v1/assets/{id}/thumb` and
  `GET /v1/assets/{id}/content?max_px=1536` proxy the bytes from Immich with the
  server's key, after the asker check (404 for hidden and missing alike).
- `POST /v1/assets/inline {asset_ids, max_px}` returns base64 data URIs for
  multimodal tool calls. It caps the count and bytes.
- `POST /v1/assets/search {query?, kind?, tags?, persons?, place?, after?, before?, channels?, include_nsfw?, limit}`
  searches images only: Gemini plus SigLIP vector legs, plus lexical over `tsv`,
  fused with RRF.
- `GET /v1/people/{id}/photographed-with` returns co-appearance counts: people
  in the same frame. `GET /v1/people/{id}/nearby` returns people photographed
  within ±2 h and 1 km of this person's photos without sharing a frame. Each
  comes with example asset ids.
- The `visual` retrieval leg joins `auto` queries when the scope admits image
  messages. Hits map asset → message through `tl.message_assets` and fuse like
  any other leg. Timeline's semantic match accepts `visual` hits as well as `vector`.

## Pipeline (`throughline images ...`)

```
discover → fetch bytes → upload to Immich → wait for Immich ML → pull (exif, place, faces, ocr, siglip vector)
        → locate (location history) → nudenet → classify kind + zero-shot tags → gemini embedding (batch) → render → status=enriched
```

- `discover` scans `tl.messages` (current versions) for attachments by source:
  - `chat.*` (Beeper): `fields.attachments` with image mime types or extensions.
    Bytes come from the Beeper Desktop API: the live message's attachment
    `srcURL` (mxc) goes through `POST /v1/assets/download`. Discord CDN URLs
    are tried first and usually have expired.
  - `t3code.messages`: attachments in T3's `statev2.sqlite`, read only. The id
    maps to `~/.t3/userdata/attachments/<id>.<ext>`.
  - `email.message`: the `fields.attachments` image parts of personal-mailbox
    senders. Bytes come through the email plugin's fetch path.
  - `hermes.*`: image parts and image file references in the raw export payload.
  - Immich itself: assets that Throughline doesn't know (`photos.immich`).
- Dedup is by sha256. Immich also refuses duplicates by checksum, and the
  existing asset id is reused.
- Upload uses `fileCreatedAt` = EXIF time or the message time, and `deviceId=throughline-<origin>`.
- Folders and tags in Immich:
  - albums `Source · Discord`, `Source · Instagram`, `Source · SMS`,
    `Source · Email`, `Source · Hermes` and `Source · T3 Code`
  - kind albums `Screenshots`, `Documents`, `Artwork` and `Memes`
  - Immich tags `kind/<kind>`, `source/<origin>` and each tag
  - The storage template is on: `{{y}}/{{y}}-{{MM}}-{{dd}}/{{filename}}`.
- Every stage is idempotent and recorded in `tl.assets.enrich`. Each stage has
  a per-run `--limit` and can be re-run alone:
  - `throughline images sync`
  - `throughline images backfill --source beeper|t3code|email|hermes --limit N`
  - `throughline images enrich --stage nsfw|tags|gemini`
  - `throughline images status`

## Implementation notes (core)

Files: `migrations/0063_images.sql`, `allowlist/0063_images.json`,
`images/store.py`, `providers/visual.py`, `retrieval/legs/visual.py`,
`retrieval/legs/image_lexical.py`, `retrieval/images.py`,
`api/routes/assets.py`; images[] in `api/routes/find.py`, `timeline.py`,
`orient.py` and `retrieval/hydrate.py` (`attach_assets`).

Database:
- `tl.assets.tsv` is set by the trigger `assets_tsv` from
  `tl.asset_document(asset, people)`: kind and tags (weight A), caption,
  place and the confirmed people's names (B), OCR (C), file name (D). Faces
  and names change outside the row, so `store.replace_faces` and
  `store.refresh_depicts` touch the row to recompute it. A restricted asker's
  keyword match uses the document without names, computed per call.
- `tl.ensure_asset_vector_index(model, dims)` builds one partial HNSW index
  per model and size (`tl.embedding_index_name('asset_vectors', model,
  dims)`); `store.put_vector` calls it with the vector's own length, and
  `tl.ensure_asset_vector_indexes()` covers every (model, dims) present.
- Gates: `api.message_assets` (`api.message_visible(message_id)`, current
  version), `api.assets` (one visible message; columns without `tsv` and
  `enrich`), `api.asset_faces` (`false`). `api.asset_visible(id)` is the same
  rule as a function.
- Functions: `api.assets_of` (response shaping), `api.visual_records` and
  `api.image_search_records` (message legs: the best image per message, then
  `tl.recall_scope` with the full hard scope), `api.visual_assets` and
  `api.image_search_assets` (asset search, optional prefiltered `asset_ids`).
  Vector functions ask the model's HNSW index for 8x (4x for assets) the
  results first and rank exactly when that does not fill the answer. All
  exclude nsfw (`nsfw >= 0.6` or tag `nsfw`) unless `include_nsfw`. As for
  text vectors, `as_of`/`known_by` are refused (`feature_not_supported`).
- Seed hooks `image_link_types` (`depicts`, inverse `depicted_in`) and
  `image_identifier_schemes` (`immich`/`face`, normalizer `lower`).
- Purge hook `message_assets` (ord 34) deletes the channel's links and the
  assets no other channel uses; depicts links go with the channel's links.
- Depicts links: `method='rule'`, `job='images.depicts'`, `job_version='1'`,
  `evidence={"asset_ids": [...]}`, one per (current message, confirmed person).

Store (`throughline.images.store`, every function takes the connection and
never commits):
- `upsert_asset(conn, *, sha256, origin, mime=None, bytes=None, width=None,
  height=None, original_name=None) -> id`: an existing sha256 keeps its
  origin and only gains missing facts. A 64-hex digest is stored lower-case.
- `set_asset(conn, asset_id, merge_enrich=None, drop_enrich=None, **cols) -> row`
  (drop_enrich: enrich keys removed before the merge): cols among
  immich_id, kind, tags, ocr_text, caption, taken_at, lat, lon, place, nsfw,
  nsfw_labels, status, duration_s, width, height, mime, bytes,
  original_name. Tags are normalised (`Sunset Beach` -> `sunset-beach`);
  `nsfw >= 0.6` adds the tag `nsfw`.
- `link_message(conn, *, channel, external_id, asset_id, ord=0,
  role='attachment', source_ref=None)`: upsert; an asset without `taken_at`
  takes the message's time; the message's depicts links are refreshed.
- `replace_faces(conn, asset_id, faces) -> count`, faces of
  `{immich_face_id, immich_person_id (None: unassigned), bbox, score, name}`.
- `refresh_depicts(conn, asset_id=None) -> {added, removed, updated}`. Identity
  changes no longer need it (0064 triggers call `tl.refresh_asset_depicts`);
  `None` remains a full repair pass.
- `asset_safe(row)`: `tl.asset_safe` in Python, plus the tag `nsfw`.
- `put_vector(conn, asset_id, model, vector)`.
- `render_text(asset_row, people_names=())`, `people_names(conn, asset_id)`.
- `assets_needing(conn, stage, limit)`: `upload` (pending, no immich_id),
  `pull` (in Immich, no `enrich.pull`), `nsfw` / `tags` / `gemini` (pulled,
  no key of the stage). A stage is done once its key is in `enrich`, an
  `{"error": ...}` value too (nsfw: only with `gave_up`); delete the key to
  run it again.

Embeddings (`throughline.providers.visual`):
- `embed_query(text, 'gemini'|'siglip', scope=...) -> (vector, model)`:
  cached in `tl.semantic_cache` under the operator `visual_query_embedding`
  (30 days), called through `providers.call` (ledger origin `query`). After
  an HTTP 40x or transport failure that provider is skipped for 10 minutes.
- `embed_images([(bytes, mime)], scope=..., origin='worker') -> (vectors,
  Usage)`: Gemini `batchEmbedContents` with `inline_data` parts (16 images
  or 16 MB per request), purpose `visual_embedding`, estimated at 258 tokens
  an image. `gemini().image_request(bytes, mime)` builds one request for the
  asynchronous batch API.
- Settings: `THROUGHLINE_VISUAL_EMBEDDING_PROVIDER` (`gemini` | `none`),
  `_MODEL` (gemini-embedding-2), `_DIMENSIONS` (1536), `_KEY` (else
  `GEMINI_API_KEY`, `GOOGLE_API_KEY`), `_BASE_URL`, `_PRICE_PER_MILLION`
  (0.20); `IMMICH_ML_URL` and `IMMICH_CLIP_MODEL` for SigLIP. Gemini's host
  must be in `THROUGHLINE_EGRESS_ALLOW`.

Retrieval:
- `visual` (auto, embedding cost): embeds the query in the first image space
  that has vectors in the scope's channels (Gemini, then SigLIP), with no call
  at all when none has. The model used is in `discovery[].detail.model`
  (with `asset_id`); a fallback adds an execution warning; with neither
  provider the leg is skipped (`visual_embeddings_unavailable`).
- `image_lexical` (auto, free): `api.image_search_records`.
- Both are pruned for a query without content tokens, left out by `raw_only`,
  and unavailable under `as_of`/`known_by` (`images_current_only`).
- Timeline's semantic pass runs `vector` and `visual`; visual hits are kept
  by their own floor per model (gemini 0.30, siglip 0.10, uncalibrated) and
  0.85 of the best visual score, at most 15. Its `involved` clause also
  follows active depicts links.
- `RecallRequest.include_nsfw` (false) and `include_images` (true; find and
  timeline set false and attach images themselves, in one statement).
- `agent_view.shrink_recall` cuts images to 3 per record and OCR to 120
  characters before it drops records.

Routes (`api/routes/assets.py`): asset routes read through the reader path
(the security matrix classifies them as reader routes; inline and search are
EXEMPT reads); the people routes call `require_unrestricted`. Bytes come
from `IMMICH_URL` (default `http://127.0.0.1:2283`) with `IMMICH_API_KEY`
(503 without it, 502 when Immich fails). `content` serves the original when
it fits `max_px` and is a browser format, else Immich's preview or a
Pillow-downscaled original (HEIC through pillow-heif when installed; Pillow
is used when present and is not yet a declared dependency).


## Running the pipeline

The commands live under `throughline lib images` (new commands go under
`lib` to keep the root help small):

```
throughline lib images status
throughline lib images sync [--limit N] [--source t3code ...]
throughline lib images backfill --source beeper|t3code|email|hermes|immich --limit N [--dry-run]
throughline lib images enrich --stage upload|pull|locate|nsfw|tags|gemini|native|native-messages --limit N
throughline lib images settings [--set key=value ...]
throughline lib images reclassify-previews [--dry-run]
throughline lib images people sync [--dry-run]
throughline lib images redescribe [--limit N]
throughline lib images beeper-requests --out req.json [--channel chat.sms] [--limit N]
throughline lib images email-requests --out req.json
throughline lib images calibrate
```

Settings come from the environment (see `throughline/images/config.py`).
Env files named by `IMAGES_ENV_FILES` fill only names that are not already
set; the default list is `~/.throughline/immich.env`, `~/.throughline/gemini.env`
and the Immich compose `.env`. Immich's Postgres (`IMMICH_DB_URL`, read only)
and its ML `/predict` endpoint (`IMMICH_ML_URL`) are published on loopback by
`~/.throughline/immich/docker-compose.override.yml` (ports 54332 and 3003).

Bytes that live on another host go through stdlib scripts that run next to
the secrets and ship back data only:

- **Beeper.** Run `beeper-requests`, then copy `throughline/images/beeper_fetch.py`
  and the request file to the host that runs Beeper Desktop and holds its token:
  `python3 beeper_fetch.py --requests R --out ./beeper-media --base-url <Beeper Desktop API URL> --token-cmd "<command that prints BEEPER_ACCESS_TOKEN>"`.
  Then rsync the output directory to `BEEPER_MEDIA_IMPORT_DIR` (default
  `~/.throughline/exports/beeper-media`). Snapchat, chats with a message
  expiry and messages flagged as ephemeral are never opened.
- **Email.** Run `email-requests`, then run `throughline/images/email_fetch.py`
  on the mail host with `COMPOSIO_API_KEY` (Composio tool
  `GMAIL_GET_ATTACHMENT`). Copy its output to `IMAGES_EMAIL_IMPORT_DIR`.
- **Hermes.** The export's `HERMES_MAX_TEXT` cuts base64 images. Re-export
  with `HERMES_MAX_TEXT=50000000` to recover them.

Zero-shot tags and kinds come from `throughline/images/tags.json` (the
vocabulary, prompts, per-tag thresholds and the meme rule); tune it there.

### Timer (not installed by default)

`~/.config/systemd/user/throughline-images-sync.service`:

```
[Unit]
Description=Throughline image pipeline (one sync pass)
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile=%h/.throughline/images-sync.env
ExecStart=/usr/local/bin/throughline lib images sync
Nice=10
TimeoutStartSec=50min
```

`~/.config/systemd/user/throughline-images-sync.timer`:

```
[Unit]
Description=Run the Throughline image pipeline every 15 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
```

`images-sync.env` holds `THROUGHLINE_DATABASE_URL` (and any IMAGES_* overrides).
Enable with `systemctl --user daemon-reload && systemctl --user enable --now throughline-images-sync.timer`.
A oneshot service never overlaps itself, and every stage has a per-run limit,
so a long backlog drains over several runs.
