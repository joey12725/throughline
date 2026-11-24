# Benchmark harness (public twin)

`bench/` is the benchmark that lives in the repository. It builds a seeded,
fully synthetic archive with planted questions and gold evidence, loads it
through the public ingest API, runs retrieval configurations ("arms") against
it over HTTP and scores what comes back. It never imports the system under
test, so the same code scores a test database, a container or a remote
deployment. Benchmarks on a real archive (BEN-1, 2, 4 to 8) plug into the
same formats but stay with whoever owns that archive; no real-corpus data or
numbers are published here.

```
python -m bench run     --load [--seed 47] [--scale 1] [--arms keyword,fused] [--scoreboard FILE] [--json]
python -m bench latency --load [--baseline FILE] [--write-baseline] [--repeats 3] [--warmup 1] [--tolerance 0.20]
```

Both read `THROUGHLINE_URL` and `THROUGHLINE_TOKEN` (an owner token).
`--load` ingests the synthetic corpus, so use it only on a disposable
instance. Without it the corpus must already be loaded.

## Corpus

`bench.scenarios.build(seed=47, scale=1)` returns a `Corpus` with `channels`,
`people`, `messages`, `links`, `questions`, plus `phases`/`link_phases` (load
phase of each message and link), `subjects`, `link_validity`, `pages` (each
page with the raw records it was written from) and `gaps`.

- Channels: `bench.chat_a`, `bench.chat_b` (two chat apps with separate
  account systems), `bench.notes`, and `bench.wiki` (a reference channel; a
  page is one `external_id` and each revision is a message with a later
  `happened_at`). `bench.wiki` is declared `record_kind: page_revision` only
  when the running system's OpenAPI document shows that channel property, and
  subject intervals are sent only when it shows `subject` on messages.
- Vocabulary: every meaningful word is invented (three consonant-vowel
  syllables closed by `k` or `x`). PostgreSQL does not stem these words and
  question templates surround them with English stop words only, so lexical
  behaviour is exact and every planted answer is unique.
- `bench/synonyms.json` maps invented words to a group id; two words of one
  group mean the same. It is the only link between a paraphrase question and
  its answer, and it is the file to give the deterministic local embedder
  (`THROUGHLINE_LOCAL_SYNONYMS=bench/synonyms.json`).
- `scale` multiplies only the distractor volume (900 messages per unit), so
  quality numbers stay comparable while latency runs on a larger archive.
- Phases: phase 1 is the archive, phase 2 holds what arrives late (backfilled
  messages and links created after a cutoff). `load(client, corpus,
  phase=None)` sends at most 5,000 messages per call; the runner loads phase
  by phase and records the database clock in between, which becomes the
  `known_by` of questions "as known after phase 1".

Twelve questions are planted per class:

| Category | What is planted | Gold |
|---|---|---|
| `exact_token` | a fact whose words are all in the question, plus a near miss | the fact |
| `paraphrase` | a fact in other words (synonym file only), half with a lexical trap | the fact |
| `cross_platform` | the same fact on both chat apps | one group, two alternatives |
| `person_handles` | one person writing under two handles, another person saying something similar | one group per handle |
| `ambiguous_person` | two people with the same display name | one group per person; discloses `ambiguous_person` |
| `cross_source_time` | an anchor in notes, the answer in chat inside its time span | anchor and answer |
| `context_dependent` | an answer turn that only reads with the turn before it | both turns |
| `changed_fact` | a value and its later correction | the latest; the old one is helpful |
| `no_answer` | a neighbouring fact only | nothing (`no_answer`) |
| `duplicate_cluster` | three occurrences of one experience, each reported three times, one unsure report | one group and one cluster per occurrence; an ambiguous member |
| `pattern` | supporting observations, counterexamples, a comparison population | one group per role, `roles` |
| `page_assisted` | raw records and a page; only the page shares words with the question | the raw records; the page is helpful |
| `page_mutation` | raw truth with a `missing`, `stale`, `contradictory` or `wrong` page | the raw record; discloses `<mutation>_page` |
| `temporal` | `subject_time` (written long after the day it is about), `revision_as_of`, `interval_relationship` (a link valid for an interval), `late_backfill` (phase 2) | the record valid at the question's time; later ones forbidden |
| `leakage` | a `fact`, a `link` or a `page_revision` that appears after the cutoff | what was known then; the later item forbidden |
| `import_gap` | a question inside a date range `bench.chat_b` never imported | nothing; `expects_incomplete`, discloses `import_gap` |

## Gold format

`bench.gold.Question` (pydantic):

| Field | Meaning |
|---|---|
| `id`, `text`, `category`, `variant` | identity and class |
| `source` | BEN-4 tag: `canary` or `structure` |
| `cutoff` | BEN-6: only data recorded before this may be used |
| `as_of` | record-time view the question asks for |
| `known_by_phase` | ingest-time view, as a load phase; `bind` fills `known_by` with the recorded instant |
| `after`, `before` | window the question is about |
| `groups[{id, required, alternatives}]` | evidence groups; any alternative covers a group (BEN-6, BEN-10) |
| `helpful` | useful but not required; never credited, never penalised |
| `no_answer` | the archive holds no answer |
| `clusters[{id, members, ambiguous}]` | reports of one experience (BEN-13) |
| `roles{supporting, counterexample, comparison}` | populations of a pattern question (BEN-14) |
| `expects_incomplete`, `expects_disclosure` | the search cannot be complete, and what must be said (BEN-20) |
| `forbidden` | records that must not appear in the result or in any stage (BEN-21) |
| `needs` | capabilities an arm must have for the question to be posed |

Evidence is named by key, never by database id: `[channel, external_id]` is
the current version of a message, `[channel, external_id, version]` one
revision. `gold.resolve(client, keys)` reads `api.message_versions` through
`POST /v1/sql` after a load and returns `{key: message id}`;
`gold.bind(question, ids, phase_times)` returns the question with ids in place
of keys. Scorers compare references by equality and accept either form.

## Metrics (`bench.score`, pure functions)

`returned` is an ordered list of delivered units: a record, or a collection of
records for a span.

| Function | Definition |
|---|---|
| `f_beta(returned, question, beta=0.5)` | F over evidence groups. A required group is one true positive however many alternatives are returned; helpful and optional records are neutral; every other unit is a false positive. |
| `group_coverage` | `all_required_covered`, `fraction`, `covered`, `missing` (BEN-10). Reported beside F0.5: a result can score high F0.5 and still lack a required group. |
| `recall_at_k` | share of required groups covered by the first k units |
| `stage_recall(stages, question)` | group recall per reported stage, for example `before_rerank`, `after_rerank`, `after_selection` (BEN-9) |
| `with_spans(records, spans, span_credit=True)` | span credit: a span containing a gold message delivers it. On by default and written into every report as `span_credit`. |
| `cluster_coverage` | share of clusters with a certain member delivered; clusters reached only through an ambiguous member are listed as `uncertain` |
| `duplicate_grouping_accuracy(predicted, question)` | pairwise agreement between predicted groups and gold clusters over certain members; `None` with fewer than two judged records |
| `role_coverage` | share of gold populations with at least `min_per_role` records |
| `abstention` | `correct_abstention`, `false_answer`, `answered` or `false_abstention` |
| `incomplete_disclosure(incomplete, disclosures, question)` | correct when the incompleteness flag is set exactly when expected and every expected term is named; a flag on a complete search is a `false_alarm` and not correct |
| `leakage(returned, question, stages)` | forbidden records in the result or in any stage |
| `citation_faithfulness(cited_ids, delivered_ids, gold)` | share of cited ids that exist, were delivered and belong to a gold group |
| `percentiles(values)` | `p50`, `p95` by linear interpolation |

## Arms and the runner

`bench.arms.ARMS` holds `Arm(name, settings, needs, run(client, question) ->
ArmResult{records, spans, stages, tokens, latency_ms, status, incomplete,
disclosures, reason, raw})`.

| Arm | What it does |
|---|---|
| `oracle` | returns one gold source per required group: the ceiling (1.0) |
| `none` | returns nothing: the floor (0) |
| `keyword`, `meaning`, `fused` | `GET /v1/search` in that mode, k 10, `before_at` set to the question's cutoff |

Nothing is ever scored zero for being unavailable:

- `arms.probe(client)` asks the system what it offers. An arm whose `needs`
  are unmet (for example `embeddings` while no model is configured, where
  `/v1/search` answers 400) is reported with `status: skipped`, a reason and
  no numbers.
- A question whose `needs` exceed the arm's capabilities (`as_of`, `known_by`,
  `versions`, `subject_time`, `links`, `pages`) is skipped for that arm and
  counted under `skipped`.

`bench.run.run(client, corpus)` returns `(report, rows)`. Per arm, overall and
per category, the report has `f05`, `group_coverage` (share of questions with
every required group covered), `group_fraction`, `recall_at_k`,
`stage_recall`, `tokens`, `tokens_per_covered_question`, `abstention`,
`false_abstention`, `disclosure`, `false_incomplete` (share of questions
without an expected gap that the arm declared incomplete), `leaked`, `cluster_coverage`,
`role_coverage` and `latency_ms` (`p50`, `p95`). F0.5 is averaged over
questions that have required groups; abstention over those that have none.
`verdicts` compares each arm with the baseline arm under the 5-point tie
rule: a difference below five points is a tie.

`bench.scoreboard.append(path, row)` writes one JSON line with the time and
the commit and never rewrites the file. `bench.latency.measure` times a
question set per arm after warm-up rounds; `latency.compare(baseline,
current, tolerance=0.20)` fails when a p50 or p95 exceeds its baseline by more
than the tolerance. Targets come from the first recorded baseline (BEN-19).

Calibration recorded in `bench/scenarios.py` for seed 47, scale 1, keyword
arm (compared exactly by `tests/test_ben_harness.py`):

| Category | F0.5 |
|---|---|
| exact_token, cross_platform, changed_fact, duplicate_cluster, page_mutation, leakage, temporal | 1.0 |
| context_dependent, cross_source_time | 0.8333 |
| pattern | 0.9091 |
| paraphrase, person_handles, ambiguous_person, page_assisted | 0.0 |

Twelve of the twin's 192 questions cannot be posed to the keyword arm and are
skipped for it.


The behaviour segments add four questions with their own measured constants
(`KEYWORD_F05_BEHAVIOUR_SEED_47`: `behaviour_episode_count` 0.0, since "how many times"
is in no message and lexical search needs every word; `behaviour_newest_relevant`
1.0; `KEYWORD_SKIPPED_BEHAVIOUR_SEED_47` 0). The twin's constants above are
unchanged.

## v1.1 suites

```
python -m bench matrix          [--load] [--arms recall,recall_rerank] [--config deployment] [--scoreboard FILE]
python -m bench resilience      [--load]
python -m bench temporal        [--load]
python -m bench latency         [--load] --operations [--baseline bench/baseline_latency.json] [--tolerance 0.20]
python -m bench librarian-value [--load] [--threshold 5]
python -m bench portability     [--load]
python -m bench behaviours      [--load] [--local-principals] [--behaviours a,b] [--scoreboard FILE]
```

After `--load` the harness also runs `scenarios.plant` (counts in
`corpus.planted`, a revision cites only sources that existed when it was
written): provenance from every
`bench.wiki` revision to the page's source records (`POST /v1/provenance`,
one call per revision), one typed claim per page with sources (the claim of
a contradictory page names the raw record it `contradicts`), the validity
interval of every validity-bounded link (`PATCH /v1/links/{id}`) and the
coverage report of the declared import gap (`POST
/v1/plugins/bench/coverage`). The target's own maintenance (its embedding
worker, which also splits pages into passages) must run after the load; the
harness cannot start it over HTTP. `scenarios.mutate(revisions, mutation)`
states how each page mutation is applied: `missing` omits the page, `stale`
stops before the fact changed, `contradictory` and `wrong` add a revision.

Every report records its configuration (`bench.configs`): name, settings and
the commit. `offline-local` is the test configuration (local embedder with
`bench/synonyms.json`, local reranker, semantic model and controller);
`deployment` records only the provider variables visible to the harness.

### Arms (`bench.arms`)

`V11_ARMS` drive `POST /v1/recall` with one switch changed each:

| Arm | Setting |
|---|---|
| `recall` | default (every available leg, fused); the ablation baseline |
| `recall_raw_only` | `raw_only: true` |
| `recall_no_expand` | `expand: false` (no spans, no dependencies) |
| `recall_rerank` | `ranker: cross_encoder` (local reranker) |
| `recall_selection_500`, `_2000`, `_8000` | `budgets.tokens` |
| `recall_pages_off` | kinds without passages, page revisions and claims; `hints.page_sources: false` |
| `recall_pages_on` | page sources on; the agent follows a page to its support |
| `recall_link_expansion` | recall saved, then `POST /v1/expand` mode `links` |
| `sessions` | session, recall, refinement through `starting_sets`, a union transform; counts `reused_statements` |
| `match` | recall a pool, keep what `POST /v1/match` judges a match |
| `librarian` | `POST /v1/librarian`, read the packet |

`CONFIGURATIONS` are the LIB-13 three with equal access and budgets
(`LIB13_BUDGETS`: 4,000 tokens, 20 s): `ordinary` (recall plus one SQL
statement for context), `equipped` (one plan: recall, context expansion,
inspection; one read of the set) and `delegated` (librarian). The same
scripted citation policy (`arms.cite`) is used by all three.

Every arm result carries the BEN-9 stages from `execution.stage_candidates`
(`before_rerank` = pool after page sources and spans, `after_rerank` = the
rerank stage's output or the same pool when no reranker ran,
`after_selection` = what was returned) as message ids, every other reported
stage and leg (`seen`, used for leakage), tokens, model cost, agent-visible
turns, cited ids and the main-agent context tokens (everything returned to
the simulated agent, at four characters a token). Aggregates add
`failure_rate`, `model_cost`, `turns`, `context_tokens` and
`citation_faithfulness`. An arm reads the system's own declarations:
`incomplete` is a truncation, a failed stage, an expired deadline or coverage
gaps in the window; a leg that is merely unavailable is disclosed, not
counted as an incomplete search.

### Suites (`bench.run`)

| Suite | Requirement | What it reports |
|---|---|---|
| `matrix` | BEN-16 | every arm's settings and metrics; verdicts against `recall` under the 5-point tie rule on F0.5 and on group coverage |
| `world_model_delta` | BEN-11 | pages on versus off on `page_assisted`, in points |
| `resilience` | BEN-12 | per mutation: share of questions whose raw record still comes back, F0.5, page-only results |
| `exploration` | BEN-13 | `group_by` episode and support: cluster coverage, duplicate grouping accuracy of the manifest groups, the manifests |
| `patterns` | BEN-14 | role coverage only |
| `temporal` | BEN-15 | per variant: subject versus record time, revision as of, interval-valid relationship, late evidence |
| `leakage` | BEN-21 | forbidden message, link-derived record and page revision absent from records and every reported stage (legs included); legs that do not honour as_of (`execution.as_of_honoured`) must not have run; `not_covered: identity_state` |
| `sufficiency` | BEN-20 | group coverage proxy, abstention, import-gap questions declared incomplete, fault cases |
| `librarian_value` | BEN-17 | paired delegated versus recall: F0.5 and coverage deltas in points, latency, cost and failure rate; `benefit` only above the threshold (5 points) |
| `lib13` | LIB-13 | the three configurations, each with citation faithfulness and context tokens |
| `lib13_failures` | LIB-13 | stale and contradictory pages, ambiguous identity (alternatives returned), import gap (coverage names it), prompt injection in stored text (returned as data, no write), invalid SQL and timeout from a scripted controller (`faults.StubController`: degraded fallback packet), permission change during a saved exploration (409 or members gone); each `disclosed` and not `confident_wrong` |
| `behaviours` | BEN-3, BEN-20, BEN-21 | all twelve behaviours, each with status (passed, failed, skipped with the missing need), metrics, disclosures and what its plant wrote |

`bench.faults` degrades the system only through public configuration: a
1 ms `deadline_ms`, an exhaustive recall over `THROUGHLINE_RECALL_EXHAUSTIVE_CAP`,
an SQL `row_cap`, an embedding endpoint at a closed loopback port, and a
scripted controller endpoint. Environment faults are applied only to a
system in the same process; against a URL they are reported skipped.

`bench.latency.measure_operations` times recall, expansion, match and
librarian apart from each other (BEN-19). `bench/baseline_latency.json` is
the first recorded baseline of the offline twin at scale 1 (in-process test
client); later runs are gated against it with a 20% tolerance, and a new
machine records its own baseline first (`--write-baseline`).

`bench.portability` (BEN-18): a `Runner.answer(question) -> {answer,
cited_ids, tool_log}`; the `http` adapter calls `POST /v1/recall`, the `cli`
adapter runs `throughline lib recall ... --json` through Typer's CliRunner
against the same target; `compare` scores both on the same questions and
reports the gap. Real agent hosts plug in by implementing `answer`.

## Behaviour scenarios

`scenarios.build` adds one segment per scenario after the twin, built from a
generator seeded apart from the twin's (the segments are the same at every
scale and the twin's messages, questions and constants do not change). Each
segment has its own `bench.behaviour_*` channels, `bench-behaviour-*` people (account
system `benchj`) and invented vocabulary; `corpus.behaviours[name]` holds its keys
and labels. Rows a scenario loads itself (the feed's late backfill) live
in `corpus.held`, never in `corpus.messages`.

`bench.behaviours.BEHAVIOURS` holds `Behaviour{name, needs, check,
plant}`. `run_all(client, corpus, phase_times, principals, available)` runs
them under the skip rule:

- a need is a feature from `arms.probe` (an OpenAPI route or a `GET
  /v1/describe` entry), `principals` (an agent token, a plugin token, a
  restricted asker: created by the owner outside HTTP, so `python -m bench
  behaviours` against a URL skips the behaviours that need them unless
  `--local-principals` is given for a disposable instance on this machine;
  tests pass `behaviours.LocalPrincipals()`), `labels` (the principals can
  label a channel: w6-disclosure installed) or `commitments` (`POST
  /v1/commitments/extract` exists: w6-commitments installed);
- an unmet need gives `skipped` with the missing need named, never a zero
  and never a failure; a part of a check whose need is unmet is named in its
  disclosures (`labels:skipped (need labels)`);
- a defined behaviour without a check gives `skipped` with reason
  `check_pending` (none is left after wave 6);
- a check (or its plant) that raises is `failed` with the reason.
- an unknown scenario name (`--behaviours`, or `names` of `run_all`) is
  refused before anything runs.

A plant writes over HTTP only what its check needs at that moment, returns
what it wrote (`planted` in the result) and writes nothing when the target
already has it:

- the owner person: the owner side (`is_me`) needs one; a fresh twin has
  none, so `bench-behaviour-owner-self` is created with `is_owner` (a target keeps
  its own owner);
- the `direction` field: the corpus keeps `direction` in the payload, the
  system reads the declared field, so the channel declares `direction` and
  its rows are re-delivered with the field (new versions, same content);
- the played media: the caption channel is declared a reference channel and
  the caption gets the played turns' interval (`ended_at`).

| Scenario | Needs | Checked by | Planted rows | Check |
|---|---|---|---|---|
| `episode_count` | recall, episodes | w4-bench-arms | three incidents of three hits | recall `group_by: episode` gives `incidents` 3, `records` 9, unit `episode`; `count(DISTINCT episode_id)` over `api.episodes_of` is 3 |
| `newest_relevant` | recall | w4-bench-arms; last contact bench-behaviours | an old statement, the newer answer, later chatter | `hints.order: newest` starts with the newer answer, never the chatter; with `api.last_contact` the answer's author's last contact is the answer, not the newest record of the shared thread |
| `consumer_exactly_once` | feeds | w4-bench-arms | a feed and its late backfill (held) | settle window 3 s / 1 s over HTTP (restored after); late backfill delivered exactly once; repeated mark a no-op with the same response; news without mark redelivers; pending 0 after mark (once per loaded instance: the first run loads the backfill) |
| `coverage_honesty` | recall, coverage | w4-bench-arms | a partial and a complete channel | inside a reported gap `not_found_in_coverage` with the gap listed and `absence_is_not_evidence`; complete channel `not_found_in_covered_sources`; never a negative assertion; a covering report closes the gap by id |
| `link_vocabulary` | link_types, principals | w4-bench-arms | four messages to link | agent free text refused; synonym folded with `type_as_written`, no duplicate on replay; undeclared plugin type a candidate; owner merge retypes and collapses duplicate ends (once per loaded instance) |
| `restricted_inference` | recall, principals | w4-bench-arms (gate); bench-behaviours (person level, labels, commitments) | three granted and four hidden messages of one person | for the restricted asker `hidden_count_leak` is 0 for SQL counts, recall hits and manifest, `api.episodes_of`, `api.coverage_for`, feed pending (HTTP and `api.pending`), link types; brief message count, top channels and recent threads, arc and rhythm counts, last contact and open loops; the circle of A for an asker granted only the direct messages (the third person and the owner absent, B first). Wave integration: with `labels`, the label `work` on the granted channel makes every one of those counts 0 and removing it restores them; with `commitments`, commitments extracted from the loops channel count (through `api.commitments` and through every page of `GET /v1/commitments`, which must agree for the owner) 0 for the guest, all of them for an asker granted that channel, 0 again once that channel is labelled and all of them once the label is removed |
| `dyad_vs_author` | recall, person_role_between | bench-behaviours | DM of A and B, group of A, B and C, owner chat with B (owner rows outgoing, no author) | `/v1/between` and recall `person_role: between` for [A, B] return exactly the eight messages of A and B from the shared threads (`dyad_both_sides` 1, none of C's); recall `persons: [A]` (author) returns A only and is not a dyad, and `between` with A alone (recall or `/v1/between`) is refused; [owner, B] returns both sides of the owner chat, the outgoing rows by direction |
| `open_loops` | open_loops | bench-behaviours | an ask each way left unanswered, an answered ask, a wound-down chat | `GET /v1/open-loops` and `GET /v1/people/{id}/open-loops` (older than a day, horizon back to the segment) return the open asks with the right direction (`owner_owes`, `they_owe`) and never the answered or wound-down ones: precision and recall against gold, a missing direction named in the disclosures |
| `media_contaminated` | media_noise, recall | bench-behaviours | a caption playing during two transcript turns, two turns about it | after `POST /v1/transcripts/noise/run` (bounded to the segment) the played turns carry `media_noise` and the others do not; played turns are absent from default recall and `/v1/around`, present with `include_ignored`; turns discussing the media stay; stored text unchanged (sha256 before, after and against the corpus) |
| `unenrolled_voice` | speaker_inbox | bench-behaviours | two `speaker_label` authors in one recording | both labels wait in the speaker inbox; attaching one to a person is refused with a client error and leaves it unattached; no appearance carries a person; assigning one gives `attribution_status` `annotated` for its turns with no person created and no person id on any row; an ignored label leaves the inbox |
| `suspected_link` | identifier_status, recall | bench-behaviours | a person's own account and a second account | with the second identifier suspected its messages are out of the timeline (side `from`), the brief count and person-scoped recall; confirmed they are in; suspected again they are out |
| `write_replay` | idempotency | bench-behaviours | three messages | the same `Idempotency-Key` and body twice for a link, a claim and an annotation: one row, receipts equal on kind, id, record_id, version, stored_hash and counts, `replayed` false then true; the key with a different body is 409 and writes nothing; `/v1/links/batch` reports each item by index (existing, created, error) |

Gold labels gain `expects_incidents`, `expects_newest`,
`expects_absence_statement` and `dyad`; `bench.score` has
`incident_count_exact`, `newest_relevant_top1`, `absence_disclosed`,
`dyad_both_sides` and `hidden_count_leak`. With `--scoreboard` the behaviours suite
appends its report (counts per status) and then one `behaviour` row per
scenario with status, reason, missing needs, metrics, disclosures, what was
planted, the configuration and the harness version.

Known result on the offline twin: every scenario passes on a first run.
`open_loops` failed with `missing_direction:owner_owes` until migration 0053
taught `api.open_loops` to read a person's one-to-one threads and channels
where the owner never wrote (each planted ask sits in a thread of its own).
`consumer_exactly_once` and `link_vocabulary` pass only on the first run
against a loaded instance, because a second run sees what the first wrote.

## Not covered

Identity state as of a past time (people and merges are rewritten in place),
anything that needs a judge model, real model quality (only deterministic
local providers run offline) and real agent hosts for BEN-18.
