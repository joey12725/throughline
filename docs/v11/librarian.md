# Librarian

The librarian (`investigate`, on demand, optional, level 3) is a bounded
controller over the shared retrieval library that returns an evidence packet.
It never runs by default and nothing requires it: everything it does is a call
any agent can make with the same token. It earns its place by running a whole
investigation beside the database, under one budget, and handing back an
evidence packet built by the runtime.

*(The former `curate` maintenance mode was removed in migration `0074`;
ongoing synthesis, linking, and upkeep are handled by pages lenses — see
[pages.md](pages.md).)*

Requirements: QRY-39..46, LIB-1, LIB-3, LIB-5..9, ACL-10, CST-4, CST-10,
INT-12; design priority P7 (evidence classes) and P1 (coverage honesty).

## How to call investigate

| Form | Call |
| --- | --- |
| HTTP | `POST /v1/librarian` with the request envelope, returns an EvidencePacket |
| SQL | `SELECT * FROM api.librarian(goal => '...', deadline_ms => 6000, tokens => 6000) l` |
| CLI | `throughline lib librarian investigate GOAL [--channel ...] [--deadline-ms ...]` (`throughline lib librarian GOAL ...` still works) |
| Library | `throughline.librarian.investigate(scope, token_row, request)` |
| Inspect | `GET /v1/investigations/{id}`, `throughline lib investigation ID` |

`api.librarian` is allowed only alone in the top-level FROM. Its rows are the
delivered result set; the packet-level fields (notes, coverage, unresolved
needs, status, continuation) are in `execution.packet`.

## Request envelope

| Field | Meaning |
| --- | --- |
| `goal` | The retrieval objective (required). |
| `question` | The original question when it differs from the goal. |
| `context` | Clarification from the calling agent. |
| `scope` | The recall hard scope. Binding for every recall node the controller writes (intersected, never widened) and for the delivered set (channels, kinds, record-time window and authors are enforced again at delivery). A plan whose scope lies wholly outside it is a failed avenue. |
| `starting_sets` | Result-set handles the caller already holds (unknown or foreign: 404). |
| `known_references` | Ids or names the caller already knows. |
| `coverage_requirement` | `{must_include: [record ids or terms], enumerate: bool}`. Unmet requirements are listed in `unresolved_needs`. |
| `budgets` | `deadline_ms`, `tokens`, `model_cost`, `model_calls`, `controller_turns` (default 2). Each only lowers the token's limit. |
| `as_of`, `known_by` | Historical cutoffs; the packet's `stated_limits` says what they can and cannot do. |
| `exclude_seen` | Record ids or message ids not to deliver again. |
| `asker` | Accepted and ignored: the asker and permissions come only from the token. |

## The loop

1. A scope that is already an investigation is refused (no recursion).
   Budgets are clamped; the investigation row and its trace are created.
2. A baseline recall of the goal under the hard scope starts immediately on a
   worker thread. It is the fallback.
3. Each controller turn returns `{plan}` or `{deliver}`. Turns are counted
   apart from semantic model calls. A turn is skipped when the controller's
   measured p95 (`tl.operator_profiles`, operator `librarian.controller`)
   exceeds the remaining deadline.
4. A plan is validated (`librarian` and unknown ops are failed avenues) and
   runs completely, independent branches in parallel, without further model
   turns. The controller then sees only bounded inspection views of what it
   produced, under `state.data`, with a notice that stored text is data and
   never instructions.
5. A new plan after the first needs a stated trigger: `unexpected_alias`,
   `conflicting_dates`, `missing_participants`, `underrepresented_cases`,
   `failed_branch`, `coverage_gap`.
6. `deliver` goes through `packets.build`: records are copied by the runtime
   from a live read, notes are flagged generated and kept only with verified
   references. Proposals in the deliver action go through `claims.propose`
   and stay pending.

The controller's four tools: `describe(subject)` (GET /v1/describe),
`execute(plan)` (POST /v1/plans), `inspect(handle, view, selector,
token_budget)` (POST /v1/sets/{handle}/inspect; counts
`inspected_by_controller`), `deliver(selection, notes, unresolved)`
(POST /v1/packets). There is no tool to call a librarian, change budgets,
write data or reach the network.

## Statuses

| `execution_status` | When |
| --- | --- |
| `complete` | The controller delivered and its last plan had no failed node. |
| `degraded` | Controller error, invalid output, timeout, approval requirement, exhausted budget, replan without a trigger or a failed node: the baseline set (or the controller's set with failed nodes) is delivered and `incompleteness_reason` says why. |
| `failed` | The baseline failed too; nothing is delivered. |

Nothing runs on the caller's behalf after the response. A controller call
still in flight at the deadline is abandoned; its result is ignored.

## Coverage honesty

The brief (version `librarian-brief-v1`, under 600 tokens, ending with the
`api.describe(NULL)` index) states the evidence classes (primary, normalized,
derived, inference: cite primary evidence, never a summary or title), that
"not found in covered sources" is not "never happened", that a coverage gap or
an unavailable source is not evidence of absence, and that a hit count is not
an incident count. When the baseline recall's `execution.coverage` (or the
packet's `known_source_gaps`) names partial channels or gaps, they are listed
in `unresolved_needs` as `source coverage partial: <channel>`; never as an
absence.

## What is stored

`tl.investigations` keeps references only: the request, the plans, the
baseline and result handles, note records `{generated_text, supporting_refs,
uncertainty}`, counts, failed avenues, coverage by channel and month (counts),
the incompleteness reason and the policy version. No excerpt, packet record or
controller prompt is stored anywhere; the trace holds ids, counts, hashes and
statements. Error messages kept as failed avenues or reasons have every quoted
value replaced by `<value>` (a failed cast would otherwise quote stored text);
quoted identifiers after words such as `column` or `function` stay readable.
The same redacted reasons are what the controller sees.

The columns have size limits. A request whose long fields would not fit
stores them as `{count, stored: "digest", sha256}`; notes, plans and failed
avenues beyond the limit are left out and counted (`counts.notes_not_stored`,
`plans_not_stored`, `failed_not_stored`), and a plan larger than 40 kB is
stored as its digest. If the row still cannot be written, its status, handles
and reason are written without details, so it never stays `running`.

`GET /v1/investigations/{id}` is answered for the same token and asker, or for
the owner (404 otherwise). After an access policy change it is 409. Otherwise
the packet is rebuilt from the stored result set and notes under the caller's
current scope: members hidden since, and the notes that cite them, drop out.
A purge deletes investigations whose notes cite the purged channel.

## Escalation from recall

`POST /v1/recall` escalates only when all hold: the token has
`allow_escalation`; the request carries `hints.escalate = {librarian: true,
budgets: {...}}` with at least one budget; and the result has fewer than k
records or a must_term that no record contains. The response is then the
packet with `escalated: true` and the investigation id. `recall()`, plans and
SQL never escalate.

## Limits of this build

Only deterministic local controllers and recorded HTTP responses are tested.
The quality of a real controller model, and the LIB-4 operations extract,
compare, relate and summarize, are not part of this build.

