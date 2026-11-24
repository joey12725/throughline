"""Annotations.

An annotation states something about a message without changing it:
effective time, effective place, location, ignore, media noise or speaker,
or any attribute registered in tl.annotation_attributes (migration 0068,
throughline.classifiers.registry), whose value is checked against its schema.
It is keyed by channel and external_id, so it survives re-import and new
versions (target_version NULL applies to every version). Precedence of the
effective value: owner > the record's own fields.ignore_reason > rule >
model, the latest within a method; an owner {ignored: false} restores a
record a source or a job flagged. Records whose effective annotation state is ignored
are excluded by default from recall (tl.recall_scope, migration 0042).

Writers (`token_or_job`):
- an owner token writes directly (method owner, origin owner);
- a job ({job, job_version, method rule|model}, or a job name with
  job_version and method keywords) writes method rule or model, origin job,
  and never writes over, supersedes or retracts an active owner annotation;
- an agent token, or any restricted scope, gets a proposal of kind
  'annotation' instead;
- plugin tokens are refused.

Functions take an app-role connection; routes authorize first. Errors:
ValueError (400), PermissionError (403), LookupError (404).
"""

from psycopg.types.json import Jsonb

from . import lineage

ATTRIBUTES = (
    "effective_time",
    "effective_place",
    "location",
    "ignore",
    "media_noise",
    "speaker",
)


def _id(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _writer(token_or_job, method=None, job_version=None):
    if isinstance(token_or_job, dict) and "kind" in token_or_job:
        kind = token_or_job["kind"]
        if kind == "plugin":
            raise PermissionError("plugins cannot annotate")
        if kind == "owner":
            return {
                "kind": "owner",
                "method": "owner",
                "origin": "owner",
                "job": None,
                "job_version": None,
                "token": token_or_job,
                "token_id": token_or_job.get("id"),
            }
        if kind == "agent":
            return {"kind": "agent", "token": token_or_job}
        raise PermissionError("token cannot annotate")
    if isinstance(token_or_job, str) and token_or_job:
        spec = {"job": token_or_job, "job_version": job_version, "method": method}
    elif isinstance(token_or_job, dict) and token_or_job.get("job"):
        spec = dict(token_or_job)
        spec.setdefault("method", method)
        spec.setdefault("job_version", job_version)
    else:
        raise ValueError("a writer is a token or a job")
    chosen = spec.get("method") or "rule"
    if chosen not in ("rule", "model"):
        raise ValueError("job annotations use method rule or model")
    if not spec.get("job_version"):
        raise ValueError("job annotations need job and job_version")
    return {
        "kind": "job",
        "method": chosen,
        "origin": "job",
        "job": str(spec["job"]),
        "job_version": str(spec["job_version"]),
        "token": None,
        "token_id": spec.get("token_id"),
    }


def resolve_target(conn, target):
    """(channel_id, external_id, current message id) of a target given as
    {message_id} (any version) or {channel, external_id}."""
    if not isinstance(target, dict):
        raise ValueError("target is {message_id} or {channel, external_id}")
    if _id(target.get("message_id")):
        row = conn.execute(
            "SELECT m.channel_id,m.external_id FROM tl.messages m WHERE m.id=%s",
            (target["message_id"],),
        ).fetchone()
    elif isinstance(target.get("channel"), str) and isinstance(
        target.get("external_id"), str
    ):
        row = conn.execute(
            "SELECT c.id AS channel_id,m.external_id FROM tl.channels c JOIN tl.messages m ON m.channel_id=c.id AND m.external_id=%s WHERE c.name=%s LIMIT 1",
            (target["external_id"], target["channel"]),
        ).fetchone()
    else:
        raise ValueError("target is {message_id} or {channel, external_id}")
    if not row:
        raise LookupError("target message not found")
    current = conn.execute(
        "SELECT id FROM tl.messages WHERE channel_id=%s AND external_id=%s AND superseded_by IS NULL",
        (row["channel_id"], row["external_id"]),
    ).fetchone()
    return row["channel_id"], row["external_id"], current["id"] if current else None


def _value(attribute, value):
    if not isinstance(attribute, str) or not attribute:
        raise ValueError("attribute must be one of " + ", ".join(ATTRIBUTES))
    if not isinstance(value, dict):
        raise ValueError("value must be an object")
    return value


def _check_value(conn, attribute, value):
    # The registry (migration 0068): any registered attribute; a registered
    # (non-core) attribute's value is checked against its value_schema.
    from .classifiers import registry

    registry.check(conn, attribute, value)
    if attribute not in ATTRIBUTES:
        return
    if not conn.execute(
        "SELECT tl.annotation_value_ok(%s,%s) AS ok", (attribute, Jsonb(value))
    ).fetchone()["ok"]:
        shapes = {
            "effective_time": "{at (with UTC offset), ended_at?, precision}",
            "effective_place": "{name, entity_id?}",
            "location": "{lat, lon, accuracy_m?}",
            "ignore": "{ignored: boolean, reason?}",
            "media_noise": "{is_noise: boolean, score?, evidence?, matched_record_id?}",
            "speaker": "{person_id}",
        }
        raise ValueError(f"{attribute} value must be {shapes[attribute]}")


def _owner_active(conn, channel_id, external_id, attribute, target_version):
    return conn.execute(
        """SELECT id FROM tl.annotations WHERE target_channel_id=%s AND target_external_id=%s
        AND attribute=%s AND status='active' AND method='owner'
        AND (target_version IS NULL OR %s::int IS NULL OR target_version=%s) LIMIT 1""",
        (channel_id, external_id, attribute, target_version, target_version),
    ).fetchone()


def _insert(
    conn,
    who,
    channel_id,
    external_id,
    attribute,
    value,
    reason,
    valid,
    target_version,
    confidence,
    current_id,
    group_annotation_id=None,
):
    row = conn.execute(
        """INSERT INTO tl.annotations(target_channel_id,target_external_id,target_version,attribute,value,
         valid_from,valid_to,valid_from_precision,valid_to_precision,method,origin,job,job_version,token_id,confidence,reason,
         group_annotation_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (
            channel_id,
            external_id,
            target_version,
            attribute,
            Jsonb(value),
            *(valid.get(k) for k in lineage.VALIDITY),
            who["method"],
            who["origin"],
            who["job"],
            who["job_version"],
            who["token_id"],
            confidence,
            reason,
            group_annotation_id,
        ),
    ).fetchone()
    if current_id is not None:
        lineage.add_provenance(
            conn,
            who["token"] or who["job"],
            "annotation",
            row["id"],
            [current_id],
            "annotates",
            who["origin"],
            who["job"],
            who["job_version"],
        )
    return row


def create(
    conn,
    token_or_job,
    target,
    attribute,
    value,
    reason=None,
    validity=None,
    target_version=None,
    confidence=None,
    *,
    method=None,
    job_version=None,
    scope=None,
    restricted=False,
    group_annotation_id=None,
):
    """Annotate a message. Returns {annotation} for a direct write,
    {annotation: None, skipped: 'owner_annotation', owner_annotation_id} when
    a job meets an active owner annotation of the same attribute, or
    {proposal} for an agent token or a restricted scope."""
    who = _writer(token_or_job, method, job_version)
    value = _value(attribute, value)
    if reason is not None and (not isinstance(reason, str) or len(reason) > 2000):
        raise ValueError("reason must be text of at most 2000 characters")
    if target_version is not None and (not _id(target_version) or target_version < 1):
        raise ValueError("target_version must be a version number")
    if confidence is not None and (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        raise ValueError("confidence must be a number from 0 to 1")
    valid = lineage.validity(validity or {})
    _check_value(conn, attribute, value)
    if who["kind"] == "agent" or restricted:
        from .proposals import propose
        from .scope import Scope

        token = who["token"] if who["kind"] == "agent" else token_or_job
        payload = {
            "target": target,
            "attribute": attribute,
            "value": value,
            "reason": reason,
            "target_version": target_version,
            "confidence": confidence,
            "method": "model",
            **{k: validity[k] for k in lineage.VALIDITY if validity and k in validity},
        }
        proposal = propose(
            conn,
            scope or Scope.from_token(token),
            token,
            "annotation",
            payload,
            {},
            "agent",
        )
        return {"proposal": proposal}
    channel_id, external_id, current_id = resolve_target(conn, target)
    if who["kind"] == "job":
        owner = _owner_active(conn, channel_id, external_id, attribute, target_version)
        if owner:
            return {
                "annotation": None,
                "skipped": "owner_annotation",
                "owner_annotation_id": owner["id"],
            }
    row = _insert(
        conn,
        who,
        channel_id,
        external_id,
        attribute,
        value,
        reason,
        valid,
        target_version,
        confidence,
        current_id,
        group_annotation_id,
    )
    return {"annotation": row}


def _lock(conn, annotation_id):
    row = conn.execute(
        "SELECT * FROM tl.annotations WHERE id=%s FOR UPDATE", (annotation_id,)
    ).fetchone()
    if not row:
        raise LookupError("annotation not found")
    return row


def _guard(who, row):
    if who["kind"] == "agent":
        raise PermissionError("agents propose annotations; they do not change them")
    if row["method"] == "owner" and who["kind"] != "owner":
        raise PermissionError("a job never overrides an owner annotation")
    if row["status"] != "active":
        raise ValueError(f"annotation is {row['status']}, not active")


def supersede(
    conn,
    token_or_job,
    annotation_id,
    value,
    reason=None,
    *,
    method=None,
    job_version=None,
):
    """Replace an active annotation's value by a new row (same target and
    attribute); the old row stays, superseded."""
    who = _writer(token_or_job, method, job_version)
    old = _lock(conn, annotation_id)
    _guard(who, old)
    _value(old["attribute"], value)
    _check_value(conn, old["attribute"], value)
    current = conn.execute(
        "SELECT id FROM tl.messages WHERE channel_id=%s AND external_id=%s AND superseded_by IS NULL",
        (old["target_channel_id"], old["target_external_id"]),
    ).fetchone()
    conn.execute(
        "UPDATE tl.annotations SET status='superseded' WHERE id=%s", (old["id"],)
    )
    row = _insert(
        conn,
        who,
        old["target_channel_id"],
        old["target_external_id"],
        old["attribute"],
        value,
        reason if reason is not None else old["reason"],
        {k: old[k] for k in lineage.VALIDITY},
        old["target_version"],
        old["confidence"],
        current["id"] if current else None,
        old.get("group_annotation_id"),
    )
    conn.execute(
        "UPDATE tl.annotations SET superseded_by=%s WHERE id=%s", (row["id"], old["id"])
    )
    return {"annotation": row, "superseded": old["id"]}


def retract(
    conn, token_or_job, annotation_id, reason=None, *, method=None, job_version=None
):
    """Mark an active annotation retracted; the row stays."""
    who = _writer(token_or_job, method, job_version)
    old = _lock(conn, annotation_id)
    _guard(who, old)
    conn.execute(
        "SELECT set_config('throughline.status_note',%s,true)", (reason or "",)
    )
    row = conn.execute(
        "UPDATE tl.annotations SET status='retracted' WHERE id=%s RETURNING *",
        (old["id"],),
    ).fetchone()
    conn.execute("SELECT set_config('throughline.status_note','',true)")
    return row


def list_for(conn, message_id, include_inactive=False):
    """Annotations of one message's record (any version), newest first."""
    row = conn.execute(
        "SELECT channel_id,external_id FROM tl.messages WHERE id=%s", (message_id,)
    ).fetchone()
    if not row:
        return []
    return conn.execute(
        "SELECT * FROM tl.annotations WHERE target_channel_id=%s AND target_external_id=%s AND (%s OR status='active') ORDER BY id DESC",
        (row["channel_id"], row["external_id"], include_inactive),
    ).fetchall()


def effective(scope, ids):
    """api.effective for message ids under the caller's scope."""
    from .query import reader_transaction

    if not isinstance(ids, list) or not all(_id(i) for i in ids):
        raise ValueError("ids must be message ids")
    with reader_transaction(scope) as conn:
        return conn.execute(
            "SELECT * FROM api.effective(%s::bigint[])", (ids,)
        ).fetchall()
