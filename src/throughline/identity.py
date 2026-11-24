"""Identity operations with owner precedence and reversible merge snapshots."""

from psycopg import sql
from psycopg.types.json import Jsonb


def event(conn, operation, actor, detail):
    return conn.execute(
        "INSERT INTO tl.identity_events(operation,asserted_by,detail) VALUES (%s,%s,%s) RETURNING id",
        (operation, actor, Jsonb(detail)),
    ).fetchone()


def attach(conn, identifier_id, person_id, actor):
    if (
        person_id is not None
        and not conn.execute(
            "SELECT 1 FROM tl.people WHERE id=%s AND merged_into IS NULL", (person_id,)
        ).fetchone()
    ):
        raise ValueError("active person required")
    row = conn.execute(
        "SELECT * FROM tl.identifiers WHERE id=%s FOR UPDATE", (identifier_id,)
    ).fetchone()
    if not row:
        raise ValueError("identifier not found")
    if (
        person_id is not None
        and actor != "owner"
        and conn.execute(
            "SELECT 1 FROM tl.people WHERE id=%s AND (is_owner OR asserted_by='owner')",
            (person_id,),
        ).fetchone()
    ):
        raise ValueError("owner assertion is protected")
    if row["asserted_by"] == "owner" and actor != "owner":
        raise ValueError("owner assertion is protected")
    if (
        row["person_id"] is not None
        and person_id is not None
        and row["person_id"] != person_id
    ):
        raise ValueError("identifier belongs to another person; merge required")
    conn.execute(
        "UPDATE tl.identifiers SET person_id=%s,asserted_by=%s WHERE id=%s",
        (person_id, actor, identifier_id),
    )
    conn.execute(
        "UPDATE tl.messages SET person_id=%s WHERE author_identifier_id=%s",
        (person_id, identifier_id),
    )
    protect_people(conn, actor, [person_id])
    event(
        conn,
        "attach" if person_id else "detach",
        actor,
        dict(identifier_id=identifier_id, person_id=person_id),
    )
    return {"ok": True}


def merge(conn, source, target, actor, override_not_same=False):
    if source == target:
        raise ValueError("cannot merge person into self")
    conn.execute("SELECT pg_advisory_xact_lock(71824002)")
    # Keep original pairs so a later split restores their meaning automatically.
    parents = {
        p["id"]: p["merged_into"]
        for p in conn.execute("SELECT id,merged_into FROM tl.people")
    }

    def root(person):
        while parents.get(person) is not None:
            person = parents[person]
        return person

    blocked = any(
        {root(p["person_a"]), root(p["person_b"])} == {source, target}
        for p in conn.execute("SELECT person_a,person_b FROM tl.not_same")
    )
    if blocked and not (actor == "owner" and override_not_same is True):
        raise ValueError("not_same pair requires explicit owner override")
    people = conn.execute(
        "SELECT * FROM tl.people WHERE id=ANY(%s) ORDER BY id FOR UPDATE",
        ([source, target],),
    ).fetchall()
    if len(people) != 2 or any(p["merged_into"] for p in people):
        raise ValueError("two active people are required")
    if (
        actor != "owner"
        and conn.execute(
            "SELECT 1 FROM tl.identifiers WHERE person_id=%s AND asserted_by='owner'",
            (source,),
        ).fetchone()
    ):
        raise ValueError("owner assertion is protected")
    if actor != "owner" and any(
        p["is_owner"] or p["asserted_by"] == "owner" for p in people
    ):
        raise ValueError("owner assertion is protected")
    # JSON snapshots preserve all fields, including timestamps and primary keys.
    snapshot = {}
    for table, condition, args in [
        ("people", "id=ANY(%s)", ([source, target],)),
        # Messages follow their author identifier, which is snapshotted below.
        # Only attributions that cannot be re-derived that way are listed, so
        # the event stays small however many messages the person wrote.
        (
            "messages",
            "person_id=%s AND (author_identifier_id IS NULL OR author_identifier_id NOT IN (SELECT id FROM tl.identifiers WHERE person_id=%s))",
            (source, source),
        ),
        ("identifiers", "person_id=%s", (source,)),
        ("person_names", "person_id=%s", (source,)),
        ("facts", "person_id=%s", (source,)),
        ("relations", "from_id=%s OR to_id=%s", (source, source)),
    ]:
        snapshot[table] = [
            r["data"]
            for r in conn.execute(
                sql.SQL(
                    "SELECT "
                    + (
                        "jsonb_build_object('id',t.id,'person_id',t.person_id)"
                        if table == "messages"
                        else "to_jsonb(t)"
                    )
                    + " data FROM tl.{} t WHERE "
                    + condition
                ).format(sql.Identifier(table)),
                args,
            )
        ]
    snapshot.update(source=source, target=target)
    for table in ("identifiers", "person_names", "facts"):
        conn.execute(
            sql.SQL("UPDATE tl.{} SET person_id=%s WHERE person_id=%s").format(
                sql.Identifier(table)
            ),
            (target, source),
        )
    conn.execute(
        "UPDATE tl.relations SET from_id=CASE WHEN from_id=%s THEN %s ELSE from_id END,to_id=CASE WHEN to_id=%s THEN %s ELSE to_id END WHERE from_id=%s OR to_id=%s",
        (source, target, source, target, source, source),
    )
    conn.execute(
        "UPDATE tl.messages SET person_id=%s WHERE person_id=%s", (target, source)
    )
    conn.execute(
        "UPDATE tl.people SET merged_into=%s,is_owner=false WHERE id=%s",
        (target, source),
    )
    if any(p["id"] == source and p["is_owner"] for p in people):
        conn.execute("UPDATE tl.people SET is_owner=true WHERE id=%s", (target,))
    return event(conn, "merge", actor, snapshot)


def split(conn, event_id, actor):
    conn.execute("SELECT pg_advisory_xact_lock(71824002)")
    e = conn.execute(
        "SELECT * FROM tl.identity_events WHERE id=%s AND operation='merge'",
        (event_id,),
    ).fetchone()
    if not e:
        raise ValueError("merge event not found")
    if e["asserted_by"] == "owner" and actor != "owner":
        raise ValueError("owner assertion is protected")
    if conn.execute(
        "SELECT 1 FROM tl.identity_events WHERE operation='split' AND detail->>'merge_event'=%s",
        (str(event_id),),
    ).fetchone():
        raise ValueError("merge already split")
    d = e["detail"]
    participants = [d["source"], d["target"]]
    if (
        conn.execute(
            "SELECT 1 FROM tl.identity_events later WHERE later.operation='merge' AND later.id>%s AND ((later.detail->>'source')::bigint=ANY(%s) OR (later.detail->>'target')::bigint=ANY(%s)) AND NOT EXISTS(SELECT 1 FROM tl.identity_events undo WHERE undo.operation='split' AND undo.detail->>'merge_event'=later.id::text)",
            (event_id, participants, participants),
        ).fetchone()
        or conn.execute(
            "SELECT 1 FROM tl.people WHERE id=%s AND merged_into IS NOT NULL",
            (d["target"],),
        ).fetchone()
    ):
        raise ValueError("split dependent merges first")
    if (
        actor != "owner"
        and conn.execute(
            "SELECT 1 FROM tl.people WHERE id=ANY(%s) AND (is_owner OR asserted_by='owner')",
            (participants,),
        ).fetchone()
    ):
        raise ValueError("owner assertion is protected")
    if (
        actor != "owner"
        and conn.execute(
            "SELECT 1 FROM tl.identifiers WHERE id=ANY(%s) AND asserted_by='owner'",
            ([row["id"] for row in d["identifiers"]],),
        ).fetchone()
    ):
        raise ValueError("owner assertion is protected")
    conn.execute(
        "UPDATE tl.people SET is_owner=false WHERE id=ANY(%s)",
        ([d["source"], d["target"]],),
    )
    for table in ("people", "identifiers", "person_names", "facts", "relations"):
        for row in d[table]:
            if table == "people":
                row = {
                    k: v
                    for k, v in row.items()
                    if k in ("id", "merged_into", "is_owner")
                }
            elif table == "identifiers" and row.get("kind") == "speaker_label":
                # Only enrolled voices become people (0049): a speaker label
                # from a snapshot taken before that rule comes back without
                # a person; its turns keep their owner speaker annotations.
                row = dict(row, person_id=None)
            names = [k for k in row if k != "id"]
            assignments = sql.SQL(",").join(
                sql.SQL("{}=r.{}").format(sql.Identifier(k), sql.Identifier(k))
                for k in names
            )
            conn.execute(
                sql.SQL(
                    "UPDATE tl.{} t SET {} FROM jsonb_populate_record(NULL::tl.{},%s) r WHERE t.id=r.id"
                ).format(sql.Identifier(table), assignments, sql.Identifier(table)),
                (Jsonb(row),),
            )
    # Messages follow their author identifier, restored above; this also
    # covers messages attributed to the target after the merge. Set-based: one
    # statement each, whatever the message count.
    conn.execute(
        "UPDATE tl.messages m SET person_id=i.person_id FROM tl.identifiers i WHERE m.author_identifier_id=i.id AND i.id=ANY(%s) AND m.person_id IS DISTINCT FROM i.person_id",
        ([i["id"] for i in d["identifiers"]],),
    )
    if d.get("messages"):
        conn.execute(
            "UPDATE tl.messages m SET person_id=r.person_id FROM jsonb_to_recordset(%s) r(id bigint,person_id bigint) WHERE m.id=r.id AND m.person_id IS DISTINCT FROM r.person_id AND NOT EXISTS(SELECT 1 FROM tl.identifiers i WHERE i.id=m.author_identifier_id AND i.kind='speaker_label')",
            (Jsonb(d["messages"]),),
        )
    event(conn, "split", actor, dict(merge_event=event_id))
    return {"ok": True}


def protect_people(conn, actor, people):
    if actor == "owner":
        ids = [p for p in people if p is not None]
        if ids:
            conn.execute(
                "UPDATE tl.people SET asserted_by='owner' WHERE id=ANY(%s)", (ids,)
            )
