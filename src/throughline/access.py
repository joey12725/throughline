"""Askers, their grants and sensitivity labels. Owner database connection only,
like tokens.

An asker is a name a token may ask on behalf of. A registered asker is either
'owner' (reads everything) or 'restricted' (reads only granted channels and
threads). The database enforces this in every reader transaction; this module
only edits the policy tables. Runtime roles can read them and never write them,
so no HTTP token can change what it or another asker sees.

Labels (migration 0051) narrow grants: a channel, a thread or a message (its
access_label, when that names a label kind) may carry labels, and a restricted
asker reads a labelled row only through an unexpired grant that allows every
one of its labels. Every label and grant change is written to
tl.access_events with its actor and purpose in the same transaction.
"""

from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from .db import connect

MODES = ("owner", "restricted")
DEFAULT_ACTOR = "owner"


def _owner_connection():
    return connect()


def _asker(conn, name):
    row = conn.execute(
        "SELECT name,mode FROM tl.askers WHERE name=%s FOR UPDATE", (name,)
    ).fetchone()
    if not row:
        raise ValueError("no asker with that name")
    return row


def _target(conn, channel, thread_id):
    """Resolve exactly one of channel name / thread id to (channel_id, thread_id)."""
    if (channel is None) == (thread_id is None):
        raise ValueError("name exactly one of channel or thread")
    if channel is not None:
        row = conn.execute(
            "SELECT id FROM tl.channels WHERE name=%s", (channel,)
        ).fetchone()
        if not row:
            raise ValueError("no channel with that name")
        return row["id"], None
    row = conn.execute("SELECT id FROM tl.threads WHERE id=%s", (thread_id,)).fetchone()
    if not row:
        raise ValueError("no thread with that id")
    return None, row["id"]


def _target_detail(channel, thread_id):
    return {"channel": channel} if channel is not None else {"thread_id": thread_id}


def _event(conn, op, asker, detail, actor):
    conn.execute(
        "INSERT INTO tl.access_events(op,asker,detail,actor) VALUES(%s,%s,%s,%s)",
        (op, asker, Jsonb(detail), actor or DEFAULT_ACTOR),
    )


def _labels(conn, labels):
    """Validate label names against tl.labels; returns them sorted, unique."""
    if isinstance(labels, str):
        labels = [part for part in labels.split(",")]
    wanted = sorted(
        {str(label).strip() for label in labels or () if str(label).strip()}
    )
    if wanted:
        known = {
            r["label"]
            for r in conn.execute(
                "SELECT label FROM tl.labels WHERE label=ANY(%s)", (wanted,)
            )
        }
        missing = [label for label in wanted if label not in known]
        if missing:
            raise ValueError("unknown label: " + ", ".join(missing))
    return wanted


def _expiry(value):
    """None, an aware datetime or an ISO 8601 text; a time without an offset
    is UTC."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("expires_at must be an ISO 8601 time") from None
    if not isinstance(value, datetime):
        raise ValueError("expires_at must be an ISO 8601 time")
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def register_asker(name, mode="restricted", actor=None):
    """Create an asker or change its mode; returns {name, mode}. A new asker
    or a mode change is audited (op 'asker')."""
    if not name or not name.strip():
        raise ValueError("asker name required")
    if mode not in MODES:
        raise ValueError("mode must be owner or restricted")
    with _owner_connection() as conn:
        # Statement triggers move the policy version even when no row changes,
        # so an unchanged asker is not written at all.
        row = conn.execute(
            "SELECT name,mode FROM tl.askers WHERE name=%s FOR UPDATE", (name,)
        ).fetchone()
        if not row:
            conn.execute("INSERT INTO tl.askers(name,mode) VALUES(%s,%s)", (name, mode))
            _event(conn, "asker", name, dict(action="register", mode=mode), actor)
        elif row["mode"] != mode:
            conn.execute("UPDATE tl.askers SET mode=%s WHERE name=%s", (mode, name))
            _event(
                conn,
                "asker",
                name,
                dict(action="mode", mode=mode, previous_mode=row["mode"]),
                actor,
            )
        return _asker(conn, name)


def remove_asker(name, actor=None):
    """Delete an asker and its grants (audited with the grants it held).
    Afterwards the name is unregistered."""
    with _owner_connection() as conn:
        _asker(conn, name)
        held = conn.execute(
            "SELECT count(*) AS n FROM tl.asker_grants WHERE asker=%s", (name,)
        ).fetchone()["n"]
        conn.execute("DELETE FROM tl.askers WHERE name=%s", (name,))
        _event(conn, "asker", name, dict(action="remove", grants_removed=held), actor)
    return dict(name=name, removed=True)


def _held(conn, asker, channel_id, thread_id):
    return conn.execute(
        "SELECT id,labels,purpose,expires_at FROM tl.asker_grants WHERE asker=%s AND channel_id IS NOT DISTINCT FROM %s AND thread_id IS NOT DISTINCT FROM %s FOR UPDATE",
        (asker, channel_id, thread_id),
    ).fetchone()


def grant(
    asker,
    channel=None,
    thread_id=None,
    labels=(),
    purpose=None,
    expires_at=None,
    actor=None,
):
    """Let a restricted asker read one channel (by name) or one thread (by id).

    labels: the label kinds this grant permits (labelled rows stay hidden
    otherwise). purpose: why the grant exists (kept in the audit).
    expires_at: when the grant stops admitting rows (None: never). Granting an
    existing target again replaces its labels, purpose and expiry; an identical
    grant writes nothing and leaves the policy version alone.
    """
    with _owner_connection() as conn:
        _asker(conn, asker)
        channel_id, thread = _target(conn, channel, thread_id)
        wanted = _labels(conn, labels)
        expiry = _expiry(expires_at)
        held = _held(conn, asker, channel_id, thread)
        detail = dict(
            _target_detail(channel, thread),
            labels=wanted,
            purpose=purpose,
            expires_at=_iso(expiry),
        )
        if not held:
            conn.execute(
                "INSERT INTO tl.asker_grants(asker,channel_id,thread_id,labels,purpose,expires_at,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    asker,
                    channel_id,
                    thread,
                    wanted,
                    purpose,
                    expiry,
                    actor or DEFAULT_ACTOR,
                ),
            )
            _event(conn, "grant", asker, detail, actor)
        elif (
            list(held["labels"]) != wanted
            or held["purpose"] != purpose
            or held["expires_at"] != expiry
        ):
            conn.execute(
                "UPDATE tl.asker_grants SET labels=%s,purpose=%s,expires_at=%s WHERE id=%s",
                (wanted, purpose, expiry, held["id"]),
            )
            _event(conn, "regrant", asker, detail, actor)
        return _describe(conn, asker)


def extend(asker, expires_at, channel=None, thread_id=None, purpose=None, actor=None):
    """Move the expiry of one held grant (None: never expires)."""
    with _owner_connection() as conn:
        _asker(conn, asker)
        channel_id, thread = _target(conn, channel, thread_id)
        expiry = _expiry(expires_at)
        held = _held(conn, asker, channel_id, thread)
        if not held:
            raise ValueError("the asker holds no such grant")
        if held["expires_at"] != expiry:
            conn.execute(
                "UPDATE tl.asker_grants SET expires_at=%s WHERE id=%s",
                (expiry, held["id"]),
            )
            _event(
                conn,
                "extend",
                asker,
                dict(
                    _target_detail(channel, thread),
                    expires_at=_iso(expiry),
                    previous_expires_at=_iso(held["expires_at"]),
                    purpose=purpose,
                ),
                actor,
            )
        return _describe(conn, asker)


def revoke(asker, channel=None, thread_id=None, purpose=None, actor=None):
    """Withdraw one grant; raises when the asker does not hold it."""
    with _owner_connection() as conn:
        _asker(conn, asker)
        channel_id, thread = _target(conn, channel, thread_id)
        removed = conn.execute(
            "DELETE FROM tl.asker_grants WHERE asker=%s AND channel_id IS NOT DISTINCT FROM %s AND thread_id IS NOT DISTINCT FROM %s RETURNING id",
            (asker, channel_id, thread),
        ).fetchall()
        if not removed:
            raise ValueError("the asker holds no such grant")
        _event(
            conn,
            "revoke",
            asker,
            dict(_target_detail(channel, thread), purpose=purpose),
            actor,
        )
        return _describe(conn, asker)


_GRANTS = (
    "SELECT g.id,g.asker,c.name AS channel,g.thread_id,g.labels,g.purpose,g.expires_at,"
    "(g.expires_at IS NOT NULL AND g.expires_at<=now()) AS expired,g.created_by,g.created_at "
    "FROM tl.asker_grants g LEFT JOIN tl.channels c ON c.id=g.channel_id"
)


def list_grants(asker=None):
    """Every grant (or one asker's) with labels, purpose and expiry."""
    with _owner_connection() as conn:
        if asker is None:
            return conn.execute(_GRANTS + " ORDER BY g.asker,g.id").fetchall()
        _asker(conn, asker)
        return conn.execute(
            _GRANTS + " WHERE g.asker=%s ORDER BY g.id", (asker,)
        ).fetchall()


_DESCRIBE = (
    "SELECT a.name,a.mode,"
    "coalesce((SELECT array_agg(c.name ORDER BY c.name) FROM tl.asker_grants g JOIN tl.channels c ON c.id=g.channel_id WHERE g.asker=a.name),'{}') AS channels,"
    "coalesce((SELECT array_agg(g.thread_id ORDER BY g.thread_id) FROM tl.asker_grants g WHERE g.asker=a.name AND g.thread_id IS NOT NULL),'{}') AS threads "
    "FROM tl.askers a"
)


def _describe(conn, name):
    return conn.execute(_DESCRIBE + " WHERE a.name=%s", (name,)).fetchone()


def list_askers():
    """Every asker with its mode, granted channel names and granted thread ids."""
    with _owner_connection() as conn:
        return conn.execute(_DESCRIBE + " ORDER BY a.name").fetchall()


def policy_version():
    """Counter that moves on every change of askers, grants, labels or token
    askers, and when a grant expires."""
    with _owner_connection() as conn:
        return conn.execute("SELECT api.policy_version() AS version").fetchone()[
            "version"
        ]


# --- labels ------------------------------------------------------------------------


def _label_name(label):
    if not isinstance(label, str) or not label.strip():
        raise ValueError("label name required")
    return label.strip()


def _target_labels(conn, channel_id, thread_id):
    if channel_id is not None:
        rows = conn.execute(
            "SELECT label FROM tl.channel_labels WHERE channel_id=%s ORDER BY label",
            (channel_id,),
        )
    else:
        rows = conn.execute(
            "SELECT label FROM tl.thread_labels WHERE thread_id=%s ORDER BY label",
            (thread_id,),
        )
    return [r["label"] for r in rows]


def _labelled(conn, channel, thread_id, channel_id, thread):
    return dict(
        _target_detail(channel, thread),
        labels=_target_labels(conn, channel_id, thread),
    )


def add_label_kind(label, description="", purpose=None, actor=None):
    """Register a label kind. Messages whose access_label names it become
    labelled at once (no re-import)."""
    name = _label_name(label)
    with _owner_connection() as conn:
        held = conn.execute(
            "SELECT label,description FROM tl.labels WHERE label=%s", (name,)
        ).fetchone()
        if held:
            if description and held["description"] != description:
                conn.execute(
                    "UPDATE tl.labels SET description=%s WHERE label=%s",
                    (description, name),
                )
                _event(
                    conn,
                    "label_kind",
                    None,
                    dict(
                        label=name,
                        description=description,
                        previous_description=held["description"],
                        purpose=purpose,
                    ),
                    actor,
                )
        else:
            try:
                with conn.transaction():
                    conn.execute(
                        "INSERT INTO tl.labels(label,description) VALUES(%s,%s)",
                        (name, description or ""),
                    )
            except Exception as error:  # CHECK on the name
                if getattr(error, "sqlstate", None) == "23514":
                    raise ValueError(
                        "label must be lower case letters, digits and _ (1-41, starting with a letter)"
                    ) from None
                raise
            _event(
                conn,
                "label_kind",
                None,
                dict(label=name, description=description or "", purpose=purpose),
                actor,
            )
        return conn.execute(
            "SELECT label,description FROM tl.labels WHERE label=%s", (name,)
        ).fetchone()


def label(label, channel=None, thread_id=None, purpose=None, actor=None):
    """Put a label on one channel (by name) or one thread (by id)."""
    name = _label_name(label)
    with _owner_connection() as conn:
        channel_id, thread = _target(conn, channel, thread_id)
        _labels(conn, [name])
        if channel_id is not None:
            added = conn.execute(
                "INSERT INTO tl.channel_labels(channel_id,label) SELECT %s,%s WHERE NOT EXISTS(SELECT 1 FROM tl.channel_labels WHERE channel_id=%s AND label=%s) RETURNING label",
                (channel_id, name, channel_id, name),
            ).fetchall()
        else:
            added = conn.execute(
                "INSERT INTO tl.thread_labels(thread_id,label) SELECT %s,%s WHERE NOT EXISTS(SELECT 1 FROM tl.thread_labels WHERE thread_id=%s AND label=%s) RETURNING label",
                (thread, name, thread, name),
            ).fetchall()
        if added:
            _event(
                conn,
                "label",
                None,
                dict(_target_detail(channel, thread), label=name, purpose=purpose),
                actor,
            )
        return _labelled(conn, channel, thread_id, channel_id, thread)


def unlabel(label, channel=None, thread_id=None, purpose=None, actor=None):
    """Remove a label from one channel or thread; raises when it is absent."""
    name = _label_name(label)
    with _owner_connection() as conn:
        channel_id, thread = _target(conn, channel, thread_id)
        if channel_id is not None:
            removed = conn.execute(
                "DELETE FROM tl.channel_labels WHERE channel_id=%s AND label=%s RETURNING label",
                (channel_id, name),
            ).fetchall()
        else:
            removed = conn.execute(
                "DELETE FROM tl.thread_labels WHERE thread_id=%s AND label=%s RETURNING label",
                (thread, name),
            ).fetchall()
        if not removed:
            raise ValueError("no such label on that target")
        _event(
            conn,
            "unlabel",
            None,
            dict(_target_detail(channel, thread), label=name, purpose=purpose),
            actor,
        )
        return _labelled(conn, channel, thread_id, channel_id, thread)


def set_label_egress(label, allowed, purpose=None, actor=None):
    """Allow or forbid content under a label to be sent to a model provider
    off this host (embedding, rerank, semantic and controller models).
    Forbidden is the default. Forbidding again does not recall what was
    already sent; vectors stored meanwhile stay. An unchanged setting writes
    nothing."""
    name = _label_name(label)
    allowed = bool(allowed)
    with _owner_connection() as conn:
        held = conn.execute(
            "SELECT egress FROM tl.labels WHERE label=%s FOR UPDATE", (name,)
        ).fetchone()
        if not held:
            raise ValueError(f"unknown label: {name}")
        if held["egress"] != allowed:
            conn.execute(
                "UPDATE tl.labels SET egress=%s WHERE label=%s", (allowed, name)
            )
            _event(
                conn,
                "label_egress",
                None,
                dict(label=name, egress=allowed, purpose=purpose),
                actor,
            )
        return dict(label=name, egress=allowed, changed=held["egress"] != allowed)


def list_labels():
    """Label kinds with the channels and threads that carry each, and
    whether content under the label may be sent to a provider off this host."""
    with _owner_connection() as conn:
        return conn.execute(
            "SELECT k.label,k.description,k.egress,"
            "coalesce((SELECT array_agg(c.name ORDER BY c.name) FROM tl.channel_labels l JOIN tl.channels c ON c.id=l.channel_id WHERE l.label=k.label),'{}') AS channels,"
            "coalesce((SELECT array_agg(l.thread_id ORDER BY l.thread_id) FROM tl.thread_labels l WHERE l.label=k.label),'{}') AS threads "
            "FROM tl.labels k ORDER BY k.label"
        ).fetchall()


def access_events(asker=None, limit=100):
    """The newest label and grant changes (owner audit)."""
    with _owner_connection() as conn:
        return conn.execute(
            "SELECT id,op,asker,detail,actor,at FROM tl.access_events WHERE %s::text IS NULL OR asker=%s ORDER BY id DESC LIMIT %s",
            (asker, asker, max(1, min(int(limit), 1000))),
        ).fetchall()
