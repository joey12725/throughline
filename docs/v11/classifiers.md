# Classifiers

Classifiers write annotations automatically. They come in two kinds, which share
one table (`tl.classifiers`), one write path and one approval flow:

| Kind | What it is | Who writes it |
|---|---|---|
| `message` | One question per arriving message, answered by Jev (yes/no or a choice) or by the configured LLM (a free-form value). No code: a JSON definition. | Anyone: a form in the interface, or an agent |
| `python` | One function over a whole thread: judge the conversation, then each participant, then single messages. It runs in a sandbox. | Agents (the owner reviews and approves) |

Migration `0068_classifiers.sql`, package `throughline.classifiers`, routes
`api/routes/classifiers.py`, CLI `throughline lib classifiers`.

## The attribute registry

Every annotation attribute is a row of `tl.annotation_attributes`. The six core
attributes (`effective_time`, `effective_place`, `location`, `ignore`,
`media_noise`, `speaker`) keep their database value checks. A registered
attribute declares a `value_schema`:

```json
{"is_automated": {"type": "boolean", "required": true},
 "kind": {"type": "enum", "values": ["receipt", "newsletter", "alert"]},
 "score": {"type": "number", "min": 0, "max": 1}}
```

Field types are `boolean`, `number`, `integer`, `string` and `enum`. Fields not
in the schema are refused. `recall_effect: "exclude"` with a boolean `flag`
keeps messages whose value has `flag = true` out of default recall. Core
attributes do this in the overlay; a registered attribute does it through a
companion `ignore` annotation, which the engine writes and retracts alongside it.

`media_noise` also declares the optional fields `kind`, `basis` and
`classifier`, which classifiers fill in.

## Annotations on threads and participants

`tl.group_annotations` holds a verdict on a whole thread, or on one participant
of a thread. A participant is `tl.participant_key(person, author identifier,
handle)`: the person when known, else the author identifier (a transcript
speaker label is one identifier per recording), else the handle. An owner
`speaker` annotation moves a turn to that person's participant.

A group annotation with a `value` covers messages. A thread annotation covers
every current message of the thread; a participant annotation covers that
participant's messages. Without a value it is a note: a verdict such as
`{"level": "partial"}` that writes nothing on messages.

`groups.fanout(thread, attribute)` makes the message annotations match the
active group annotations. Each message takes its value from the most specific
active group annotation:

    participant (owner) > thread (owner) > participant (job) > thread (job)

Within a tier, the newest wins. Each derived message annotation carries
`group_annotation_id` and is written by the group annotation's writer. The
owner's corrections become owner annotations, so a later job never flags those
turns again (the transcript noise job skips them too). An owner annotation
written directly on a message always wins over a derived one.

When the owner overrides through `POST /v1/group-annotations` (for example
"this speaker is not media"), the correction holds when the classifier runs
again and when new turns arrive.

## Message classifiers

```json
{"name": "automated_email", "kind": "message", "channels": ["email.message"],
 "attribute": {"name": "automated", "value_schema": {"is_automated": {"type": "boolean", "required": true},
               "score": {"type": "number", "min": 0, "max": 1}}},
 "spec": {"worker": "jev", "question": "Was this sent by an automated system?",
          "answer": "yes_no", "threshold": 0.7, "value": {"is_automated": true}}}
```

| Field | Meaning |
|---|---|
| `worker` | `jev` (cheap and structured) or `llm` (returns `{annotate, value, confidence, reason}`; the value is checked against the schema) |
| `answer` | Jev only. `yes_no` writes `value` when p ≥ `threshold`; `choice` writes `values[option]` for the most likely option |
| `context_messages` | 0–20 earlier messages of the same thread, shown with each message |
| `min_chars`, `max_per_run`, `max_cost_per_run` | Limits |
| `backfill` | `all`, `<n>d` or an ISO date. Older messages are judged too, after new ones (owner only) |

A new message classifier starts at the newest message, so only messages that
arrive afterwards are judged unless you backfill. The cursor moves only past
messages that were actually judged. A provider outage or a spend limit stops the
run, and the next tick resumes from the same message.

## Python classifiers

The whole contract is `src/throughline/classifiers/runtime/throughline_classify.py`,
which `GET /v1/classifiers/guide` returns along with worked examples
(`examples/background_media.py`, `examples/automated_senders.py`).

```python
from throughline_classify import classifier

@classifier(grain="thread")
def classify(thread, ctx):
    if not thread.others:
        return []
    level = ctx.jev.choose("How much of this is media audio?", thread.render(), ["none", "partial", "full"])
    if level.answer == "full":
        return [thread.mark(is_noise=True, confidence=level.p, detail={"level": "full"})]
    ...
```

- **Objects:** `Thread`, `Participant` and `Message` are read-only.
  `render()` produces text sized for a model call: long threads are sampled into
  evenly spaced runs of turns, with a note saying how many were left out.
- **Marks:** `thread.mark()`, `participant.mark()` and `message.mark()` write a
  value; `.note()` records a verdict without writing on messages. The last mark
  for a target wins. A `level` in a thread mark's detail is the conversation's
  verdict.
- **`ctx`:** `ctx.jev.probability / yes_no / choose / ask_many / decide`,
  `ctx.llm.json / ask` and `ctx.log`. These calls are the only way out of the
  sandbox.

### When threads are judged

A thread is judged once no message has arrived for `settle_minutes` (default
20), and again only when new messages arrive. After a code change, threads whose
last judgement errored are judged again; other threads keep their verdict
unless you retract the classifier's marks (`retract-marks`), which judges
everything again. Marks are checked before anything is written: an invalid mark
writes nothing for that thread and is recorded as an error.

### Sandbox

The child process gets an empty environment: no database URL, key or token. It
talks JSON lines over stdin and stdout. Under bubblewrap (`/usr/bin/bwrap`,
`THROUGHLINE_CLASSIFIER_SANDBOX=auto`, the default) it also has no network and
sees only `/usr`, the Python installation and its own read-only directory. It
runs under CPU, memory, file and descriptor limits.

Without bubblewrap (containers without user namespaces) it runs as a plain
process with the same limits and the runtime's import allow-list.
`GET /v1/classifiers` reports which mode is in use, and
`THROUGHLINE_CLASSIFIER_SANDBOX=bwrap` refuses to run without bubblewrap. A
static check (`classifiers.check`) refuses imports outside the allow-list,
`open`, `eval`, `getattr` and similar builtins, and any attribute starting with
`__`. Text that is held on this machine (`tl.labels.egress`) is removed before
the code sees the thread.

## Spend

Before every call (an estimate) and after it (the real cost), a run checks:
- its own `max_cost_per_run` and call limit,
- the classifier's `monthly_cap_usd` (default $5),
- `THROUGHLINE_MONTHLY_MODEL_CAP_USD`.

In a tick, Jev message classifiers that judge the same message the same way
share one call, and `ctx.jev.ask_many` asks several questions about one text in
one call (see [shared-calls.md](shared-calls.md)).

Calls are recorded in `tl.model_ledger` with purpose `classifier.<name>`. Try
runs use `classifier.<name>.try` and are capped at 25 items and $0.25. Jev's key
is `OPENROUTER_API_KEY`; without it, `THROUGHLINE_OPENROUTER_KEY_COMMAND` is run
once per process and its output is kept in memory only.

## Approval

- **The owner's definitions** are active at once.
- **An agent's definition** is `proposed`, and so is an agent's change to an
  active one, until the owner approves it (`POST /v1/classifiers/{name}/status
  {"status": "active"}`).
- **Owner only:** run, backfill, retract-marks and group annotations.
- **Open to the owner and agents:** check, try and draft. They need an
  unrestricted token.

## For agents

1. `GET /v1/classifiers/guide`
2. `GET /v1/annotation-attributes`
3. Write the definition.
4. `POST /v1/classifiers/check`: fix what it names.
5. `POST /v1/classifiers/try` with `{"sample": {"limit": 5}}`, or with
   `thread_ids` or `message_ids`.
6. `POST /v1/classifiers`.

In the app, `POST /v1/classifiers/draft {goal, kind?, channels?}` has the LLM
write the definition from a plain-language goal. It runs the checks and sends
their errors back to the model for up to two repairs.
`THROUGHLINE_CLASSIFIER_DRAFT_MODEL` and `_BASE_URL` choose the model; otherwise
the lens LLM settings apply.

## Running

The maintenance timer runs `throughline lib classifiers tick`, which runs every
active classifier once. `throughline lib classifiers run <name>` runs one now,
in-process.

## Routes

| Route | Who | |
|---|---|---|
| `GET /v1/classifiers/guide` | owner, agent | contract, workflow, examples |
| `GET /v1/classifiers`, `GET /v1/classifiers/{name}`, `GET /v1/classifiers/{name}/results` | owner, agent | |
| `POST /v1/classifiers/check`, `/try`, `/draft` | owner, agent | nothing stored |
| `POST /v1/classifiers` | owner (active), agent (proposed) | receipt |
| `POST /v1/classifiers/{name}/status`, `/run`, `/backfill`, `/retract-marks` | owner | receipt |
| `GET /v1/annotation-attributes`, `POST /v1/annotation-attributes`, `POST /v1/annotation-attributes/{name}/retire` | owner, agent (retire: owner) | |
| `GET /v1/threads/{id}/classification` | owner, agent | review view: participants, turns, flags, verdicts |
| `POST /v1/group-annotations`, `POST /v1/group-annotations/{id}/retract` | owner | correction, copied onto turns |
