"""The helper model's system prompt and the first user message.

The system prompt has two parts. The first is the same for every run (role,
the read-only SQL surface, the view spec format and the lens catalogue) and
is sent first so a provider can cache it. The second is derived from the live
catalogue under the request's scope: the channels it sees, their views, the
allow-listed api functions and builtins. Nothing in either part is a row
value.
"""

import json

from . import lenses
from .catalog import SEMANTIC
from .tools import DATA_NOTICE, message_columns_text

ROLE = (
    """You are the query helper beside the SQL editor of Throughline, a self-hosted personal archive (chats, e-mail, calendar, health, location, feeds, notes). You help its owner write, fix and understand read-only SQL and pick a picture for the result. Your product is always one SQL statement plus, when a picture helps, one view spec; the owner sees them, edits them, runs them and saves them.

Work with the tools: look up the catalogue (describe), profile a channel, find tested recipes, try statements with run_sql, check a picture with chart. Keep tool use short: a few calls, then finish. Always end the run by calling final exactly once, with a statement that validates. Never invent channels, columns or functions: the catalogue lists them. Answer in plain, short markdown: what the statement does, what it assumes, and what the owner might change. Do not claim facts about the data that the tools did not show you.

"""
    + DATA_NOTICE
)

SURFACE = """The SQL surface (read-only):
- Exactly one SELECT (WITH ... SELECT is fine). No INSERT/UPDATE/DELETE/DDL, no SELECT INTO, no locking clauses, no bind parameters ($1): write literals inline and double every ' in a string literal.
- Every relation is schema-qualified in schema api: api.messages, api.channels, api.people, api.threads, api.links, api.entities, api.recipes, ... and one view per channel, api.ch_<channel name with . replaced by _>, which has the message columns plus one typed column per declared field (text, bigint, float8, boolean, timestamptz, jsonb), named exactly as the field. Without the view, read a field as (fields->>'name')::float8 on api.messages WHERE channel = '<name>'.
- Message columns: {columns}. happened_at is the record's own time (UTC); never select tsv.
- Functions: api.<name> for the allow-listed api functions below (row-returning ones go in FROM, scalar ones such as api.describe in the select list), and the unqualified builtins below. Nothing else runs; schema-qualified builtins (pg_catalog.x) are refused.
- Semantic functions ({semantic}) run models or embeddings and may need an approval: avoid them unless asked.
- numeric results arrive as strings: cast every aggregate you chart (avg(x)::float8, count(*)::bigint). Alias every output column with a unique name.
- Bound the work: a happened_at window, the channel view instead of api.messages, aggregates rather than raw rows, LIMIT for lists. The row cap is 1000 for a run.
- Disclosure tiers: at the schema tier no row value reaches you (run_sql shows columns, types, counts and errors only; profile counts only). At the samples tier you see at most 20 rows with cells clipped to 200 characters. Never ask the owner to paste data."""


def static_text():
    builtin_note = (
        "Builtins and api functions are listed in the second part of this prompt."
    )
    return "\n\n".join(
        [
            ROLE,
            SURFACE.format(
                columns=message_columns_text(),
                semantic=", ".join("api." + s for s in SEMANTIC),
            ),
            builtin_note,
            lenses.prompt_text(),
            "Finish with final(answer, statement, spec, notes). The statement must be one SELECT that validates (final checks it; an invalid one is returned to you once). spec is a view spec as above, or null when a table is enough. notes are short assumptions (dates, units, which channel).",
        ]
    )


def dynamic_text(catalog):
    channels = catalog.channels()
    lines = ["The catalogue for this asker (live, read under its scope):"]
    lines.append(
        "Channels (name -> view; describe('channel:<name>') lists the fields): "
        + "; ".join(
            f"{c['name']} -> {c['view'] or 'api.messages WHERE channel = ' + json.dumps(c['name'])}"
            + (f" ({len(c['fields'])} fields)" if c["fields"] else "")
            for c in channels
        )
    )
    lines.append("Allow-listed api functions: " + ", ".join(catalog.api_functions()))
    lines.append("Allow-listed builtins: " + ", ".join(catalog.builtins()))
    subjects = [
        s.get("subject")
        for s in catalog.index().get("subjects") or []
        if isinstance(s, dict)
    ]
    if subjects:
        lines.append(
            "describe subjects: "
            + ", ".join(str(s) for s in subjects if not str(s).startswith("recipe:"))
        )
    return "\n".join(lines)


VERBS = {
    "ask": "Answer the owner's question with a statement (and a view spec when a picture helps).",
    "fix": "The statement in the editor fails. Find out why (run_sql shows the error), fix it with the smallest change, and explain the fix in one or two sentences.",
    "explain": "Explain what the statement (or the selected part) does, in plain English, structurally: what it reads, filters, groups, computes and returns. Return the statement unchanged in final.",
    "refine": "Change the statement in the editor as the owner asks, keeping everything else.",
}


def user_text(request, tier, max_steps):
    parts = [VERBS.get(request["verb"], VERBS["ask"])]
    if request.get("question"):
        parts.append("Owner: " + request["question"])
    if request.get("statement"):
        parts.append(
            "Statement in the editor:\n```sql\n" + request["statement"] + "\n```"
        )
    if request.get("selection"):
        parts.append("Selected text:\n```sql\n" + request["selection"] + "\n```")
    if request.get("error"):
        error = request["error"]
        parts.append(
            "The last run failed: "
            + json.dumps({k: error.get(k) for k in ("code", "message") if error.get(k)})
        )
    if request.get("result"):
        parts.append(
            "Shape of the last result (no rows): "
            + json.dumps(request["result"], default=str)
        )
    if request.get("scope"):
        parts.append(
            "The owner's current time scope: "
            + json.dumps(request["scope"], default=str)
        )
    if request.get("channel"):
        parts.append("Channel in focus: " + request["channel"])
    if request.get("history"):
        lines = [f"{h['role']}: {h['text']}" for h in request["history"]]
        parts.append("Earlier in this conversation:\n" + "\n".join(lines))
    parts.append(
        f"Disclosure tier: {tier}. You have at most {max_steps} tool calls before final."
    )
    return "\n\n".join(parts)
