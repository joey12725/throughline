"""Annotations on a whole thread or on one participant of a thread, and their
fan-out onto messages (migration 0068).

A group annotation with a value covers messages: a thread annotation covers
every current message of the thread, a participant annotation the messages
whose tl.participant_key is its participant_key. A group annotation without a
value is a note (a verdict that covers nothing, e.g. level 'partial').

fanout(thread, attribute) makes the message annotations match the active
group annotations. The message value comes from the most specific active
group annotation, owner first:

    participant (owner) > thread (owner) > participant (job) > thread (job)

newest first within a tier. Each derived message annotation carries
group_annotation_id; it is written by the group annotation's writer (the
owner's own derived annotations are owner annotations, so later jobs never
re-flag those messages). An active owner annotation written directly on a
message always wins: a job-derived one is not written over it.

A registered attribute with recall_effect 'exclude' gets a companion
`ignore` annotation (same writer, same group_annotation_id) on each message
whose value has flag=true; it is retracted with the derived annotation.
"""

import json

from psycopg.types.json import Jsonb

from .. import annotations
from . import registry

TIER = {("participant", True): 0, ("thread", True): 1, ("participant", False): 2, ("thread", False): 3}


def _writer_of(row):
    if row["method"] == "owner":
        return {"kind": "owner", "id": row["token_id"]}
    return {"job": row["job"], "job_version": row["job_version"], "method": row["method"]}


def _same(a, b):
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def active(conn, thread_id, attribute=None, classifier_id=None):
    clauses, params = ["thread_id=%s", "status='active'"], [thread_id]
    if attribute:
        clauses.append("attribute=%s")
        params.append(attribute)
    if classifier_id is not None:
        clauses.append("classifier_id=%s")
        params.append(classifier_id)
    return [
        dict(r)
        for r in conn.execute(
            f"SELECT * FROM tl.group_annotations WHERE {' AND '.join(clauses)} ORDER BY id", params
        )
    ]


def insert(conn, *, target_kind, thread_id, participant_key, attribute, value, detail, confidence,
           reason, writer, classifier_id=None):
    owner = writer.get("kind") == "owner"
    row = conn.execute(
        """INSERT INTO tl.group_annotations(target_kind,thread_id,participant_key,attribute,value,detail,
         confidence,reason,method,origin,job,job_version,classifier_id,token_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (
            target_kind, thread_id, participant_key, attribute,
            Jsonb(value) if value is not None else None, Jsonb(detail or {}),
            confidence, (reason or None) and str(reason)[:2000],
            "owner" if owner else writer["method"], "owner" if owner else "job",
            None if owner else writer["job"], None if owner else writer["job_version"],
            classifier_id, writer.get("id") if owner else writer.get("token_id"),
        ),
    ).fetchone()
    return dict(row)


def set_status(conn, ids, status, superseded_by=None):
    if ids:
        conn.execute(
            "UPDATE tl.group_annotations SET status=%s,superseded_by=coalesce(%s,superseded_by) WHERE id=ANY(%s) AND status='active'",
            (status, superseded_by, list(ids)),
        )


def _thread_messages(conn, thread_id):
    return [
        dict(r)
        for r in conn.execute(
            """SELECT m.id,m.channel_id,m.external_id,
             tl.participant_key(coalesce(sp.person_id,m.person_id),
               CASE WHEN sp.person_id IS NULL THEN m.author_identifier_id END,m.author_handle) AS participant
            FROM tl.messages m
            LEFT JOIN LATERAL (SELECT (a.value->>'person_id')::bigint AS person_id FROM tl.annotations a
              WHERE a.target_channel_id=m.channel_id AND a.target_external_id=m.external_id
               AND a.attribute='speaker' AND a.status='active'
              ORDER BY a.method='owner' DESC,a.id DESC LIMIT 1) sp ON true
            WHERE m.thread_id=%s AND m.superseded_by IS NULL ORDER BY m.id""",
            (thread_id,),
        )
    ]


def _derived(conn, thread_id, attribute):
    """Active derived annotations on the thread's messages for this attribute
    (and their companion ignores): {(channel_id, external_id): [rows]}."""
    out = {}
    for r in conn.execute(
        """SELECT a.* FROM tl.annotations a JOIN tl.group_annotations g ON g.id=a.group_annotation_id
        WHERE g.thread_id=%s AND g.attribute=%s AND a.status='active'""",
        (thread_id, attribute),
    ):
        out.setdefault((r["target_channel_id"], r["target_external_id"]), []).append(dict(r))
    return out


def _retract(conn, rows, note):
    ids = [r["id"] for r in rows]
    if not ids:
        return 0
    conn.execute("SELECT set_config('throughline.status_note',%s,true)", (note,))
    conn.execute("UPDATE tl.annotations SET status='retracted' WHERE id=ANY(%s) AND status='active'", (ids,))
    conn.execute("SELECT set_config('throughline.status_note','',true)")
    return len(ids)


def write_message(conn, writer, message, attr_row, value, *, confidence=None, reason=None, group_annotation_id=None):
    """Write one message annotation (and the companion ignore of an exclude
    attribute). Returns the number of annotations written (0 when an owner
    annotation of the attribute stands)."""
    target = {"message_id": message["id"]}
    done = annotations.create(
        conn, writer, target, attr_row["name"], value, reason, None, None, confidence,
        group_annotation_id=group_annotation_id,
    )
    if not done.get("annotation"):
        return 0
    n = 1
    if attr_row["origin"] != "core" and registry.excludes(attr_row, value):
        extra = annotations.create(
            conn, writer, target, "ignore", {"ignored": True, "reason": attr_row["name"]},
            f"{attr_row['name']}: {reason or 'flagged'}"[:2000], None, None, confidence,
            group_annotation_id=group_annotation_id,
        )
        n += 1 if extra.get("annotation") else 0
    return n


def fanout(conn, thread_id, attribute):
    """Make the thread's message annotations of `attribute` match its active
    group annotations. Returns {written, retracted, kept, covered}."""
    attr_row = registry.require(conn, attribute)
    groups = [g for g in active(conn, thread_id, attribute) if g["value"] is not None]
    messages = _thread_messages(conn, thread_id)
    derived = _derived(conn, thread_id, attribute)
    stats = {"written": 0, "retracted": 0, "kept": 0, "covered": 0}

    def best_for(m):
        best = None
        for g in groups:
            if g["target_kind"] == "participant" and g["participant_key"] != m["participant"]:
                continue
            rank = (TIER[(g["target_kind"], g["method"] == "owner")], -g["id"])
            if best is None or rank < best[0]:
                best = (rank, g)
        return best[1] if best else None

    seen = set()
    for m in messages:
        key = (m["channel_id"], m["external_id"])
        seen.add(key)
        want = best_for(m)
        have = derived.get(key, [])
        main = [r for r in have if r["attribute"] == attribute]
        if want is not None:
            stats["covered"] += 1
        if want is not None and main and all(r["group_annotation_id"] == want["id"] for r in have):
            stats["kept"] += 1
            continue
        stats["retracted"] += _retract(conn, have, f"group annotation changed (thread {thread_id})")
        if want is not None:
            stats["written"] += write_message(
                conn, _writer_of(want), m, attr_row, want["value"],
                confidence=want["confidence"], reason=want["reason"], group_annotation_id=want["id"],
            )
    # Derived annotations of records no longer current in the thread stay as
    # they are: they follow their record (keyed by external_id).
    return stats


def apply_marks(conn, *, classifier, thread_id, marks, writer, valid_keys):
    """Replace this classifier's group annotations on a thread with `marks`
    (already validated), supersede what changed, retract what is gone, then
    fan out. Message-target marks are written directly. Returns stats."""
    attribute = classifier["attribute"]
    attr_row = registry.require(conn, attribute)
    current = {(g["target_kind"], g["participant_key"]): g for g in active(conn, thread_id, attribute, classifier["id"])}
    stats = {"group_written": 0, "group_kept": 0, "group_retracted": 0, "message_written": 0}
    wanted = {}
    message_marks = []
    for mk in marks:
        if mk["target"] == "message":
            message_marks.append(mk)
            continue
        key = (mk["target"], mk["key"] if mk["target"] == "participant" else None)
        wanted[key] = mk  # the last mark for a target wins
    for key, mk in wanted.items():
        old = current.pop(key, None)
        if old and _same(old["value"], mk["value"]) and _same(old["detail"], mk["detail"] or {}) and old["job_version"] == writer["job_version"]:
            stats["group_kept"] += 1
            continue
        row = insert(
            conn, target_kind=key[0], thread_id=thread_id, participant_key=key[1], attribute=attribute,
            value=mk["value"], detail=mk["detail"], confidence=mk["confidence"], reason=mk["reason"],
            writer=writer, classifier_id=classifier["id"],
        )
        if old:
            set_status(conn, [old["id"]], "superseded", row["id"])
        stats["group_written"] += 1
    set_status(conn, [g["id"] for g in current.values()], "retracted")
    stats["group_retracted"] += len(current)
    # Direct message marks of this classifier: replace the previous ones.
    job_rows = conn.execute(
        """SELECT a.* FROM tl.annotations a JOIN tl.messages m ON m.channel_id=a.target_channel_id AND m.external_id=a.target_external_id
        WHERE m.thread_id=%s AND a.job=%s AND a.group_annotation_id IS NULL AND a.status='active'""",
        (thread_id, writer["job"]),
    ).fetchall()
    _retract(conn, [dict(r) for r in job_rows], "classifier re-run")
    by_id = {m["id"]: m for m in _thread_messages(conn, thread_id)}
    for mk in message_marks:
        m = by_id.get(mk["key"])
        if m is None or mk["key"] not in valid_keys["messages"]:
            continue
        stats["message_written"] += write_message(
            conn, writer, m, attr_row, mk["value"], confidence=mk["confidence"], reason=mk["reason"])
    stats.update({f"fanout_{k}": v for k, v in fanout(conn, thread_id, attribute).items()})
    return stats


def owner_set(conn, token, *, thread_id, attribute, participant_key=None, value=None, detail=None, reason=None):
    """The owner's own verdict on a thread or participant: replaces the
    owner's previous one on that target, then fans out."""
    if (token or {}).get("kind") != "owner":
        raise PermissionError("only the owner writes group annotations directly")
    attr_row = registry.require(conn, attribute)
    if value is not None:
        annotations._check_value(conn, attribute, value)
    if not conn.execute("SELECT 1 FROM tl.threads WHERE id=%s", (thread_id,)).fetchone():
        raise LookupError("thread not found")
    if participant_key is not None and participant_key not in {m["participant"] for m in _thread_messages(conn, thread_id)}:
        raise LookupError("no such participant in this thread")
    kind = "participant" if participant_key else "thread"
    old = [
        g for g in active(conn, thread_id, attribute)
        if g["method"] == "owner" and g["target_kind"] == kind and g["participant_key"] == participant_key
    ]
    row = insert(
        conn, target_kind=kind, thread_id=thread_id, participant_key=participant_key, attribute=attr_row["name"],
        value=value, detail=detail or {}, confidence=None, reason=reason, writer={"kind": "owner", "id": token.get("id")},
    )
    set_status(conn, [g["id"] for g in old], "superseded", row["id"])
    return {"group_annotation": row, "fanout": fanout(conn, thread_id, attribute)}


def retract(conn, token, group_annotation_id):
    row = conn.execute("SELECT * FROM tl.group_annotations WHERE id=%s", (group_annotation_id,)).fetchone()
    if not row:
        raise LookupError("group annotation not found")
    if row["method"] == "owner" and (token or {}).get("kind") != "owner":
        raise PermissionError("only the owner retracts an owner group annotation")
    if (token or {}).get("kind") not in ("owner",):
        raise PermissionError("only the owner retracts group annotations")
    set_status(conn, [row["id"]], "retracted")
    return {"retracted": row["id"], "fanout": fanout(conn, row["thread_id"], row["attribute"])}


def for_thread(conn, thread_id):
    """Every active group annotation of a thread, with how many messages each covers."""
    rows = active(conn, thread_id)
    counts = {
        r["group_annotation_id"]: r["n"]
        for r in conn.execute(
            """SELECT a.group_annotation_id,count(*) AS n FROM tl.annotations a
            WHERE a.group_annotation_id=ANY(%s) AND a.status='active' GROUP BY 1""",
            ([r["id"] for r in rows],),
        )
    }
    for r in rows:
        r["messages_covered"] = counts.get(r["id"], 0)
    return rows
