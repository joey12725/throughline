"""Group stage (order 50, QRY-30).

When request.group_by is set, every hit gets its group key in
Candidate.groups and the response carries ctx.manifest:

  {by, unit, groups: [{key, count, record_ids[, singleton]}],
   records, incidents, omitted}

Grains: conversation, thread, channel, day, month, year, person, entity,
support (the independent group of the dedup stage), episode. unit is
'record' for every grain except 'episode'; records is the number of hit
records; incidents is the number of episode groups for the episode grain
and null otherwise (a record count is never put in the incidents field).
omitted counts grouped candidates that did not reach the response (rank or
budget). The manifest is built from the records recall finally returns.

Episode grain: one api.episodes_of statement over the hit message ids gives
the stable episode id ('ep:<thread_id>:<first_message_id>'). Records that
are not messages (pages' passages, claims, entities, links) form their own
singleton group keyed by record_id, with singleton true. If api.episodes_of
is absent, hits are grouped by thread with a 1,800 s gap between
consecutive hits and execution.warnings gets 'episode_fallback'.
"""

from ... import query as query_module
from ...db import connect
from .. import selection
from ..scope_sql import lit
from . import Stage
from .dedup import load_facts

EPISODES_CAP = 10000


def episodes_available(ctx):
    """Whether api.episodes_of exists and is on the query allow-list
    (catalogue metadata only)."""
    if "episodes_of" not in query_module.FUNCTIONS:
        return False
    try:
        with connect("app") as conn:
            row = conn.execute(
                "SELECT to_regprocedure('api.episodes_of(bigint[])') IS NOT NULL AS ok"
            ).fetchone()
    except Exception:
        return False
    return bool(row["ok"])


def episode_keys(ctx, hits):
    """{record_id: episode id or None (singleton)}."""
    messages = {}
    for c in hits:
        mid = selection.message_ref(c)
        if mid is not None:
            messages[c.record_id] = mid
    keys = {c.record_id: None for c in hits}
    if not messages:
        return keys
    if episodes_available(ctx):
        ids = sorted(set(messages.values()))[:EPISODES_CAP]
        rows = ctx.sql(
            "SELECT e.message_id, e.episode_id FROM api.episodes_of("
            + lit(ids)
            + ") e",
            cap=len(ids) + 1,
        )
        found = {r["message_id"]: r["episode_id"] for r in rows}
        for record_id, mid in messages.items():
            keys[record_id] = found.get(mid)
        return keys
    ctx.execution.warn("episode_fallback")
    keys.update(
        selection.fallback_episodes([c for c in hits if c.record_id in messages])
    )
    return keys


class Group(Stage):
    name = "group"
    order = 50
    exact = True

    def enabled(self, ctx):
        return bool(ctx.request.group_by)

    def run(self, ctx, candidates):
        by = ctx.request.group_by
        if by not in selection.GRAINS:
            ctx.execution.warn("unknown_group_by")
            ctx.execution.degrade("group", "unknown_group_by")
            return candidates
        hits = [c for c in candidates if c.role != "dependency"]
        load_facts(ctx, hits)
        if by == "episode":
            keys = episode_keys(ctx, hits)
        else:
            keys = {c.record_id: selection.group_key(c, by) for c in hits}
        for c in hits:
            key = keys.get(c.record_id)
            c.groups = [key if key is not None else c.record_id]
        planned = {c.record_id for c in hits}

        def finish(ctx, records):
            returned = [
                r
                for r in records
                if r.get("record_id") in planned and r.get("role") != "dependency"
            ]
            ids = {r["record_id"] for r in returned}
            by_id = {c.record_id: c for c in hits}
            manifest = selection.group_manifest(
                [by_id[r["record_id"]] for r in returned],
                by,
                keys=keys,
            )
            manifest["omitted"] = len(planned - ids)
            ctx.manifest = manifest

        ctx.after_recall.append(finish)
        return candidates


STAGE = Group()
