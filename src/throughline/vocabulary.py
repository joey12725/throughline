"""The closed link-type registry (LNK-1, LNK-2, IDN-6).

Every link type is registered in tl.link_types. The database trigger
tl.link_type_gate (migration 0042) normalises every written type, folds
synonyms, registers types for owner writers and declared plugin types, holds
undeclared plugin types as candidates and refuses everything else, so
free-text link types are refused or folded whichever writer is used. This
module is the owner's side: register, merge (with duplicate collapse) and
retire types, and list them with counts.

Functions take an app-role connection whose token setting names the writer;
the owner-only rule is checked here and the route checks it again.
"""

STATUSES = ("active", "candidate", "retired")
SOURCE_ORIGINS = ("plugin", "legacy", "source_candidate")


def _owner(token):
    if not (isinstance(token, dict) and token.get("kind") == "owner"):
        raise PermissionError("only the owner changes the link-type registry")


def normalize(conn, raw):
    """The registry name of a raw type (total: always a valid name)."""
    if not isinstance(raw, str):
        raise ValueError("a link type is a string")
    return conn.execute("SELECT tl.normalize_link_type(%s) AS t", (raw,)).fetchone()[
        "t"
    ]


def resolve(conn, raw):
    """The canonical type of a raw name, or None when it is not registered."""
    return conn.execute("SELECT tl.resolve_link_type(%s) AS t", (raw,)).fetchone()["t"]


def _row(conn, name):
    return conn.execute(
        "SELECT t.*,coalesce((SELECT array_agg(s.alias ORDER BY s.alias) FROM tl.link_type_synonyms s WHERE s.type=t.type),'{}') AS synonyms FROM tl.link_types t WHERE t.type=%s",
        (name,),
    ).fetchone()


def _text(value, name, limit=2000):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{name} must be text of at most {limit} characters")
    return value


def register(conn, token, type, description=None, inverse=None):
    """Register a type active (origin owner); a candidate or retired type of
    the same name becomes active. A synonym alias cannot be registered."""
    _owner(token)
    name = normalize(conn, type)
    description = _text(description, "description")
    if inverse is not None:
        inverse = normalize(conn, _text(inverse, "inverse", 200))
    alias = conn.execute(
        "SELECT type FROM tl.link_type_synonyms WHERE alias=%s", (name,)
    ).fetchone()
    if alias:
        raise ValueError(f"{name} is a synonym of {alias['type']}")
    conn.execute(
        """INSERT INTO tl.link_types(type,description,inverse,status,origin,first_seen)
        VALUES (%s,%s,%s,'active','owner',now())
        ON CONFLICT (type) DO UPDATE SET status='active',
         origin=CASE WHEN tl.link_types.status='active' THEN tl.link_types.origin ELSE 'owner' END,
         description=coalesce(EXCLUDED.description,tl.link_types.description),
         inverse=coalesce(EXCLUDED.inverse,tl.link_types.inverse)""",
        (name, description, inverse),
    )
    return _row(conn, name)


def merge(conn, token, type, into):
    """Fold `type` into `into` in one transaction (the caller's).

    Creates the synonym (and repoints synonyms of `type`), re-types every link
    of `type` (tl.link_history keeps each image, type_as_written keeps the
    original), then collapses duplicates: in every group of active links with
    the same type, ends and method that a re-typed link joined, all but the
    oldest are superseded by the oldest. `type` is retired (a candidate is
    removed). Returns {type, into, retyped, collapsed}.
    """
    _owner(token)
    name = normalize(conn, type)
    target = resolve(conn, into) or normalize(conn, into)
    if name == target:
        raise ValueError("a type cannot be merged into itself")
    row = conn.execute(
        "SELECT status FROM tl.link_types WHERE type=%s", (target,)
    ).fetchone()
    if not row or row["status"] != "active":
        raise ValueError(f"merge target {target} is not an active link type")
    current = conn.execute(
        "SELECT * FROM tl.link_types WHERE type=%s FOR UPDATE", (name,)
    ).fetchone()
    alias = conn.execute(
        "SELECT type FROM tl.link_type_synonyms WHERE alias=%s", (name,)
    ).fetchone()
    if not current and not alias:
        raise LookupError(f"link type {name} is not registered")
    conn.execute(
        "UPDATE tl.link_type_synonyms SET type=%s WHERE type=%s", (target, name)
    )
    conn.execute(
        """INSERT INTO tl.link_type_synonyms(alias,type,origin) VALUES (%s,%s,'owner')
        ON CONFLICT (alias) DO UPDATE SET type=EXCLUDED.type""",
        (name, target),
    )
    retyped = [
        r["id"]
        for r in conn.execute(
            "UPDATE tl.links SET type=%s,type_as_written=coalesce(type_as_written,type) WHERE type=%s RETURNING id",
            (target, name),
        )
    ]
    collapsed = 0
    if retyped:
        collapsed = len(
            conn.execute(
                """WITH groups AS (
                 SELECT l.id,first_value(l.id) OVER (PARTITION BY l.from_kind,l.from_id,l.to_kind,l.to_id,l.method ORDER BY l.created_at,l.id) AS keep,
                  bool_or(l.id=ANY(%(retyped)s)) OVER (PARTITION BY l.from_kind,l.from_id,l.to_kind,l.to_id,l.method) AS touched
                 FROM tl.links l WHERE l.type=%(target)s AND coalesce(l.status,'active')='active')
                UPDATE tl.links l SET status='superseded',superseded_by=g.keep
                FROM groups g WHERE l.id=g.id AND g.touched AND g.id<>g.keep RETURNING l.id""",
                {"retyped": retyped, "target": target},
            ).fetchall()
        )
    if current:
        if current["status"] == "candidate":
            conn.execute("DELETE FROM tl.link_types WHERE type=%s", (name,))
        else:
            conn.execute(
                "UPDATE tl.link_types SET status='retired' WHERE type=%s", (name,)
            )
    return {
        "type": name,
        "into": target,
        "retyped": len(retyped),
        "collapsed": collapsed,
    }


def retire(conn, token, type, into=None):
    """Retire a type. A type that imports depend on (origin plugin, legacy or
    source_candidate, or with an active source link) needs `into`, and the
    retirement becomes a merge, so later imports are folded. Others become
    retired: rule, model and agent writers are then refused for it; an owner
    writer is still accepted (recorded in last_written_while_retired)."""
    _owner(token)
    name = normalize(conn, type)
    row = conn.execute(
        "SELECT * FROM tl.link_types WHERE type=%s FOR UPDATE", (name,)
    ).fetchone()
    if not row:
        raise LookupError(f"link type {name} is not registered")
    if into is not None:
        return merge(conn, token, name, into)
    sourced = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM tl.links WHERE type=%s AND method='source' AND coalesce(status,'active')='active') AS x",
        (name,),
    ).fetchone()["x"]
    if row["origin"] in SOURCE_ORIGINS or sourced:
        raise ValueError(
            f"{name} is used by imports; retire it with into=<replacement> (a merge)"
        )
    conn.execute("UPDATE tl.link_types SET status='retired' WHERE type=%s", (name,))
    return {"type": name, "into": None, "retyped": 0, "collapsed": 0}


def list_types(conn, status=None, with_counts=False):
    """Registry rows (unrestricted callers only: candidates, retired types and
    counts over every link). retired_but_written marks retired types an import
    wrote after retirement."""
    if status is not None and status not in STATUSES:
        raise ValueError("status must be one of " + ", ".join(STATUSES))
    counts = (
        ",(SELECT count(*) FROM tl.links l WHERE l.type=t.type AND coalesce(l.status,'active')='active') AS active_links"
        ",(SELECT count(*) FROM tl.links l WHERE l.type=t.type) AS links"
        ",(SELECT count(*) FROM tl.links l WHERE l.type=t.type AND l.method='source') AS source_links"
        if with_counts
        else ""
    )
    return conn.execute(
        "SELECT t.type,t.description,t.inverse,t.status,t.origin,t.plugin,t.first_seen,t.last_written_while_retired,t.created_at,"
        "t.status='retired' AND t.last_written_while_retired IS NOT NULL AS retired_but_written,"
        "coalesce((SELECT array_agg(s.alias ORDER BY s.alias) FROM tl.link_type_synonyms s WHERE s.type=t.type),'{}') AS synonyms"
        + counts
        + " FROM tl.link_types t WHERE (%s::text IS NULL OR t.status=%s) ORDER BY t.type",
        (status, status),
    ).fetchall()
