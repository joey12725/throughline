# Transcripts

Module `throughline.transcripts`, routes `throughline.api.routes.transcripts`,
CLI `throughline lib transcripts noise run|status` and
`throughline lib transcripts speakers list|assign|ignore`, migration
`0049_transcripts.sql`, provider operation registry in
`throughline.providers.base`. design priority P4c (per-segment media-noise flags),
P4d (speaker inbox, only enrolled voices become people) and P4e (a recording
is never split); IDN-6, IDN-8, MED-3, LIB-4, ARC-6, CST-1; scenarios
`media_contaminated` and `unenrolled_voice`.

A **recording** is one thread of a `conversation_whole_thread` channel (for
example a Wave transcript); its **turns** (segments) are the thread's
messages, authored by a `speaker_label` (an unenrolled voice, such as
"Speaker 2") or a `voice_profile` (an enrolled voice), with `fields.start_s`,
`end_s`, `speaker_label`, `ignore_reason` and `place`.

**A transcript is never rewritten.** Every flag is an annotation
beside the turn (see [annotations](annotations.md)); the text, payload and times stay
as delivered.

## What is flagged and why

A turn is media noise when the microphone picked up media that was playing
(song lyrics, show dialogue, applause) instead of people talking. Such turns
are flagged per segment, never per recording: a recording with one sung
chorus keeps every other turn.

`run_noise` judges each recording that has not been judged since its last
ingest (`tl.transcript_noise_state`). For every current turn it collects, in
one statement, the media records whose played interval overlaps the turn's
effective time: messages of `record_kind` `reference` channels (plays from
Plex, YouTube, Spotify and the like, with caption or transcript text), at
most five per turn. The effective time is the turn's active `effective_time`
annotation (owner first), else `happened_at`, to `ended_at` (else
`start + end_s - start_s`).

The turns go to the semantic operation `classify_media_noise` through
`providers.call`:

    items  [{id, text, context: {overlapping_media: [{record_id, text, title}], duration_s}}]
    output media | conversation | uncertain, with evidence offsets, score and
           (for media) matched_record_id

The deterministic local rule (the default) decides, in order:

1. The best word 4-gram Jaccard between the turn and a window of the text of
   media playing at that time is at least `THROUGHLINE_MEDIA_NOISE_OVERLAP`
   (0.5): **media**, with `matched_record_id`.
2. Markers (music symbols, `[music]`, `[applause]`, `[singing]`) or lyric-like
   repetition (at least half of the word 4-grams repeat, over at least 40
   words): **media**.
3. The turn names the title of a playing work, or talks about a song, show
   or video, but overlaps little: **conversation**. Conversation about media
   is kept.
4. Otherwise **conversation**.

A media turn gets `media_noise {is_noise: true, score, evidence,
matched_record_id}` with job `transcripts`, the job version and method `rule`
(local model) or `model` (any other). Flagged turns are excluded by default,
like ignored ones: recall, person functions, feeds and the librarian's curate mode skip
them; `include_ignored` brings them back, and `api.effective` shows
`media_noise` and the reason. A later run that judges a flagged turn again
does not duplicate the flag; when the job now judges it conversation, its own
earlier flag is superseded by `{is_noise: false}`.

## Fail open

Every failure leaves the transcript as it is and says so:

- a provider error, a timeout or an invalid remote answer (the registry
  validates outputs; an invalid one fails the batch) writes no flag for that
  batch;
- a non-local provider needs an estimate and `cost.authorize` before the
  first call; over the limit the run makes no call, writes nothing and
  returns the pending approval (`approval_id`); passing it back as
  `policy.approval_id` once approved runs the same request;
- no provider at all is reported as `provider_unavailable`.

The run status is then `partial`, `failures` lists each reason, and the
recording's cursor does not advance, so the next run judges it again. Nothing
is hidden by a failure: a turn is excluded only by a flag that was written.

## Owner un-flagging

The owner decides last. An owner `media_noise {is_noise: false}` (for
example "we were singing along") or `ignore {ignored: false}` on a turn is
seen before judging: the turn is skipped and counted in `owner_kept`, so a
job never re-flags it, across re-imports and new versions (annotations are
keyed by channel and external id). Annotations also refuse a job write over
any active owner annotation of the same attribute.

## Calibration against the one-hour rule

A rule of thumb from real use is that a recording of over an hour needs noise handling. Here
it orders and calibrates, it never cuts: `min_duration_s` defaults to 0, so
every recording is covered and flagged per segment. Recordings of an hour or
more are judged first; the result reports `by_length_bucket`
(`under_10m`, `10m_to_1h`, `1h_or_more`: recordings, turns, flagged) so the
share of flags by length can be checked against the rule.

## Running it

    POST /v1/transcripts/noise/run {policy?, dry_run?}      owner
    GET  /v1/transcripts/noise/status                       owner, agent (unrestricted)
    throughline lib transcripts noise run [--policy JSON] [--dry-run]

Policy: `channels` (default every `conversation_whole_thread` channel),
`media_channels` (default every reference channel), `since` (recordings
ending at or after), `min_duration_s` (0), `max_items` (5,000 turns, whole
recordings only), `max_model_calls` (32), `max_cost` (the token's limit, else
0.10), `batch_size` (50 for a remote model), `approval_id`. The result:
`{recordings, turns_judged, flagged, by_length_bucket, model_calls, cost,
status (done | partial | max_items), failures, owner_kept, already_flagged,
pending_recordings, method, run_id}`. `dry_run` counts and estimates; it
calls nothing and writes nothing. Spend is in the model ledger with origin
`job`; `tl.transcript_noise_runs` keeps counts per run, never text.

## Speaker inbox and enrolment

**Only enrolled voices become people.** An identifier of kind
`speaker_label` never carries a person. The rule is on the identifier kind
itself (`CHECK (kind <> 'speaker_label' OR person_id IS NULL)` plus a
trigger with the message "only enrolled voices become people; enrol a voice
profile or annotate the speaker"), so it holds for every role, including the
owner, with or without an `identifier_schemes` row. `voice_profile`
identifiers attach as usual.

Migration 0049 first converted every existing label attachment: the label
was detached (its turns lost the person), and each attached turn got an owner
`speaker` annotation with that person, unless it already had one. No person
was created from a label; an identity event `speaker_label_conversion` lists
what was converted.

The inbox lists what is left to resolve:

    GET /v1/transcripts/speakers?max_results=100          unrestricted only
    throughline lib transcripts speakers list

One row per label and recording: `identifier_id, thread_id,
recording_title, label, turns, total_s, first_at, last_at,
sample_message_ids` (the first three turns) and `annotated` (every turn
already has a speaker annotation). Labels with status `ignored` never appear
(IDN-6), nor does a label in a recording where the owner ignored every one
of its turns. `api.speaker_inbox` returns nothing to a restricted asker and is not
on the SQL allow-list (level owner; served by the route only).

Resolutions (owner tokens; one transaction each; idempotency keys and
receipts through the write contract):

- `POST /v1/transcripts/speakers/assign {thread_id, label, person_id}`:
  owner `speaker {person_id}` annotations on that label's turns in that
  recording. The turns show the person in `api.effective` (unrestricted) and
  as attribution status `annotated`, never as a confirmed author.
- `POST /v1/transcripts/speakers/ignore {thread_id, label, reason}`: owner
  `ignore {ignored: true, reason}` annotations on the label's turns (for
  example a television voice) and status `ignored` on the label once no other
  recording has an unignored turn of it (Wave names labels per recording). Either way
  the label's row for that recording leaves the inbox; a label identifier
  shared with other recordings keeps its status and its other rows.

**Late enrolment (IDN-8).** When a voice is enrolled later and the source
sends the same turns again with a `voice_profile` author, they become new
versions of the same records, resolved through the normal identifier path to
the person. Nothing is re-imported, and annotations follow the records.

## A recording is one unit

`api.episodes_of`, recall spans, `group_by: "episode"` and conversations
treat a recording of a whole-thread channel as one episode and one
conversation, however long the gaps between turns. The tests check this
against the owning packages; a failure there is a defect of that package.

## Provider operation registry

`SemanticModel.apply(operation, ...)` runs registered operations only.
`providers.register_operation(name, outputs, local=fn, prompt=text,
fields={name: type})` registers one at import time of the package that owns
it, without editing provider files. `match` is built in and unchanged
(`SemanticModel.operations` still names the built-in `("match",)`;
`registered_operations()` lists all). `extract`, `compare`, `relate` and
`summarize` stay reserved and refused. The local rule model calls the
registered local function and checks its outputs; the HTTP model appends the
registered prompt to the semantic system prompt, sends the items (with their
`context`) in the JSON `data` field stated to be data, never instructions,
and validates every answer against the registered outputs and declared
fields: an invalid answer fails the call, so callers fail open.
