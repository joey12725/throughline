"""Set-oriented ingest with transaction-scoped locks and immutable versions."""

import hashlib
import json
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from . import identity_quality
from .lineage import VALIDITY, validity
from .models import Channel, Message

def quality_rank(value):
    """Rank of an identity quality (higher is better; unlisted or None is 0).
    A first delivery under a new key of an aliased record creates a version
    only when its quality ranks strictly higher than the current version's."""
    return identity_quality.rank(value)


class CodedError(ValueError):
    """A 400 with a stable machine-readable code next to the detail text."""

    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code


def payload_digest(message, external_id=None):
    """MSG-2 payload hash of a validated message.

    aliases and expected_version are outside the hash, so replay equality is
    unchanged by them. An item resolved through an alias is hashed under its
    canonical external id, the id the stored version carries.
    """
    hashed = message.model_dump(mode="json")
    if external_id is not None:
        hashed["external_id"] = external_id
    if not hashed["thread"]["metadata"]:
        del hashed["thread"]["metadata"]
    # A message without subject time hashes exactly as before the field
    # existed, so stored records are not re-versioned.
    if hashed["subject"] is None:
        del hashed["subject"]
    return hashlib.sha256(
        json.dumps(
            hashed,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def register_channel(conn, data):
    c = Channel.model_validate(data)
    existing = conn.execute(
        "SELECT * FROM tl.channels WHERE name=%s", (c.name,)
    ).fetchone()
    if existing and existing["plugin"] != c.plugin:
        raise ValueError("channel belongs to another plugin")
    values = c.model_dump()
    if values["record_kind"] is None:
        # Not declared: keep the stored kind while is_reference is unchanged,
        # otherwise derive it from is_reference.
        values["record_kind"] = (
            existing["record_kind"]
            if existing and existing["is_reference"] == c.is_reference
            else "reference"
            if c.is_reference
            else "life_event"
        )
    # The owner decides which channels are embedded: a plugin's declaration
    # is the default of a new channel and never overrides a stored choice.
    values["embed"] = (
        existing["embed"]
        if existing
        else True
        if values["embed"] is None
        else values["embed"]
    )
    # Roles and their knobs: a declaration sets them, silence keeps what is
    # stored (an owner may have set them for a plugin that declares none).
    for name in ("role", "orient_priority", "context_window_min"):
        if values[name] is None and existing:
            values[name] = existing.get(name)
    resolved = dict(values)
    values["fields"] = Jsonb(values["fields"])
    for name in ("passage_text_paths", "provenance_link_types"):
        if values[name] is not None:
            values[name] = Jsonb(values[name])
    names = list(values)
    from psycopg import sql

    conn.execute(
        sql.SQL(
            "INSERT INTO tl.channels ({}) VALUES ({}) ON CONFLICT(name) DO UPDATE SET {}"
        ).format(
            sql.SQL(",").join(map(sql.Identifier, names)),
            sql.SQL(",").join(sql.Placeholder() * len(names)),
            sql.SQL(",").join(
                sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(n), sql.Identifier(n))
                for n in names
                if n != "name"
            ),
        ),
        list(values.values()),
    )
    conn.execute("SELECT tl.refresh_channel(%s)", (c.name,))
    if existing and any(
        existing[k] != getattr(c, k)
        for k in (
            "conversation_gap_s",
            "conversation_whole_thread",
            "conversation_max_tokens",
        )
    ):
        # Same lock as ingest: without it this regroup could read a snapshot
        # older than an in-flight batch and then overwrite that batch's chunks.
        # Only this channel is locked and materialized, so lock order is safe.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (existing["id"],))
        tids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM tl.threads WHERE channel_id=%s ORDER BY id",
                (existing["id"],),
            )
        ]
        conn.execute("SELECT tl.renumber_threads(%s::bigint[])", (tids,))
        conn.execute("SELECT tl.regroup_conversations(%s::bigint[])", (tids,))
        conn.execute(
            "DELETE FROM tl.dirty_threads WHERE thread_id=ANY(%s::bigint[])", (tids,)
        )
    return resolved


def normalize_author(conn, author, schemes):
    system, kind, value = author.system, author.kind, author.value.strip()
    scheme = schemes.get((system, kind))
    if scheme:
        system, kind = scheme["same_system"] or system, scheme["same_kind"] or kind
        normalizer = scheme["normalizer"]
        if normalizer == "trim":
            target_scheme = schemes.get((system, kind))
            if target_scheme and target_scheme["normalizer"] != "trim":
                normalizer = target_scheme["normalizer"]
    else:
        normalizer = (
            "email"
            if kind == "email"
            else "phone"
            if kind in ("phone", "number")
            else "trim"
        )
    if normalizer in ("email", "lower"):
        norm = value.lower()
    elif normalizer == "phone":
        norm = "".join(c for c in value if c.isdigit())
    else:
        norm = value
    return system, kind, value, norm


def validate_fields(message, channel):
    declared = {f["name"]: f["type"] for f in channel["fields"]}
    for name, value in message.fields.items():
        if name not in declared:
            raise ValueError(f"undeclared field: {name}")
        if value is None:
            continue
        typ = declared[name]
        valid = {
            "text": lambda: isinstance(value, str),
            "integer": lambda: type(value) is int,
            "number": lambda: type(value) in (int, float),
            "boolean": lambda: type(value) is bool,
            "json": lambda: True,
            "timestamp": lambda: (
                isinstance(value, str)
                and datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset()
                is not None
            ),
        }[typ]()
        if not valid:
            raise ValueError(f"field {name} must have type {typ}")


def _ingest_pass(conn, batch, items, plugin_scope=None, *, resolve_identity=True):
    """One pass over (index, data) items. Returns (result, deferred items,
    resolved keys [(index, channel_id, canonical external id)])."""
    channels = {
        r["name"]: r
        for r in conn.execute(
            "SELECT * FROM tl.channels WHERE name=ANY(%s)",
            (
                list(
                    {
                        m.get("channel")
                        for _, m in items
                        if isinstance(m, dict) and isinstance(m.get("channel"), str)
                    }
                ),
            ),
        ).fetchall()
    }
    checked = []
    result = dict(
        received=len(items),
        inserted=0,
        new_versions=0,
        unchanged=0,
        rejected=[],
        aliased=[],
    )
    for index, data in items:
        try:
            m = Message.model_validate(data)
            if m.channel not in channels:
                raise ValueError("channel is not registered")
            c = channels[m.channel]
            if c["plugin"] != batch.plugin or (
                plugin_scope and batch.plugin != plugin_scope
            ):
                raise ValueError("channel belongs to another plugin")
            validate_fields(m, c)
            checked.append((index, m, c))
        except (ValueError, TypeError) as exc:
            result["rejected"].append(
                dict(
                    index=index,
                    external_id=data.get("external_id")
                    if isinstance(data, dict)
                    and isinstance(data.get("external_id"), str)
                    else None,
                    error=str(exc),
                )
            )
    # Lock channels in one global order: protects shared external IDs and concurrent
    # thread creation, while allowing unrelated sources to ingest concurrently.
    # The same lock serializes alias and delivery state of the channel.
    for cid in sorted({c["id"] for _, _, c in checked}):
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (cid,))
    valid, deferred, resolved_keys, effects = _resolve(conn, checked, result)
    threads = {}
    for m, c, _, _ in valid:
        key = (c["id"], m.thread.external_id)
        title, metadata = threads.get(key, (None, {}))
        incoming_metadata = m.thread.metadata
        if {"ignored", "asserted_by"} & incoming_metadata.keys():
            # Source context must not overwrite core identity decisions. Keep
            # the complete original object under an explicitly source namespace.
            incoming_metadata = {
                k: v
                for k, v in m.thread.metadata.items()
                if k not in {"ignored", "asserted_by"}
            }
            incoming_metadata["source_metadata"] = m.thread.metadata
        metadata.update(incoming_metadata)
        threads[key] = (
            m.thread.title if m.thread.title is not None else title,
            metadata,
        )
    if threads:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO tl.threads(channel_id,external_id,title,metadata) VALUES (%s,%s,%s,%s) ON CONFLICT(channel_id,external_id) DO UPDATE SET title=coalesce(EXCLUDED.title,tl.threads.title),metadata=tl.threads.metadata || EXCLUDED.metadata",
                [
                    (cid, ext, title, Jsonb(metadata))
                    for (cid, ext), (title, metadata) in threads.items()
                ],
            )
    thread_ids = {
        (r["channel_id"], r["external_id"]): r["id"]
        for r in conn.execute(
            "SELECT id,channel_id,external_id FROM tl.threads WHERE channel_id=ANY(%s) AND external_id=ANY(%s)",
            (
                list({c["id"] for _, c, _, _ in valid}),
                list({m.thread.external_id for m, _, _, _ in valid}),
            ),
        ).fetchall()
    }
    schemes = {
        (r["system"], r["kind"]): r
        for r in conn.execute("SELECT * FROM tl.identifier_schemes")
    }
    author_cache = {}
    for m, _, _, _ in valid:
        if m.author and resolve_identity:
            key = (m.author.system, m.author.kind, m.author.value)
            if key not in author_cache:
                author_cache[key] = normalize_author(conn, m.author, schemes)
    authors = set(author_cache.values())
    if authors:
        # The display name an author is shown with becomes the identifier's
        # label when it has none yet (onboarding's people resolver reads it).
        names = {}
        for m, _, _, _ in valid:
            if m.author and resolve_identity and m.author.display_name:
                a = author_cache[(m.author.system, m.author.kind, m.author.value)]
                names.setdefault(a, m.author.display_name.strip()[:200] or None)
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO tl.identifiers(system,kind,value,value_norm,label) VALUES (%s,%s,%s,%s,%s) ON CONFLICT(system,kind,value_norm) DO UPDATE SET label=EXCLUDED.label WHERE tl.identifiers.label IS NULL AND EXCLUDED.label IS NOT NULL",
                [(*a, names.get(a)) for a in authors],
            )
    identities = {}
    if authors:
        identities = {
            (r["system"], r["kind"], r["value_norm"]): r
            for r in conn.execute(
                "SELECT i.id,i.system,i.kind,i.value_norm,i.person_id FROM tl.identifiers i JOIN unnest(%s::text[],%s::text[],%s::text[]) a(system,kind,norm) ON (i.system,i.kind,i.value_norm)=(a.system,a.kind,a.norm)",
                (
                    [a[0] for a in authors],
                    [a[1] for a in authors],
                    [a[3] for a in authors],
                ),
            )
        }
    # COPY staging is shared by neither sessions nor requests.
    conn.execute(
        "CREATE TEMP TABLE incoming (seq int,channel_id bigint,external_id text,version int,old_id bigint,thread_id bigint,happened_at timestamptz,happened_offset_min int,ended_at timestamptz,author_handle text,author_identifier_id bigint,person_id bigint,content text,images jsonb,fields jsonb,lat float8,lon float8,loc_accuracy_m real,loc_method text,loc_reason text,access_label text,payload_hash text,subject_from timestamptz,subject_to timestamptz,subject_from_precision text,subject_to_precision text,payload jsonb) ON COMMIT DROP"
    )
    conn.execute(
        "CREATE TEMP TABLE incoming_envelopes(channel_id bigint,external_id text,version int,envelope jsonb) ON COMMIT DROP"
    )
    rows = []
    envelopes = []
    # Repeated keys inside a batch are applied in delivery order, including
    # edits: _resolve deferred them to the next pass.
    for m, c, digest, old in valid:
        # Location follows the record kind: a source location is kept for
        # life events and derived records, and only life events may take one
        # from the track. For every other case its absence is not a gap.
        kind = c["record_kind"]
        location = m.location if kind in ("life_event", "derived") else None
        subject = m.subject
        ident = None
        if m.author and resolve_identity:
            a = author_cache[(m.author.system, m.author.kind, m.author.value)]
            ident = identities[(a[0], a[1], a[3])]
        result["new_versions" if old else "inserted"] += 1
        envelope = m.model_dump(
            mode="json",
            include={
                "thread",
                "happened_at",
                "ended_at",
                "author",
                "location",
                "subject",
            },
        )
        if envelope["subject"] is None:
            del envelope["subject"]
        envelopes.append(
            (
                c["id"],
                m.external_id,
                old["version"] + 1 if old else 1,
                Jsonb(envelope),
            )
        )
        rows.append(
            (
                len(rows),
                c["id"],
                m.external_id,
                old["version"] + 1 if old else 1,
                old["id"] if old else None,
                thread_ids[(c["id"], m.thread.external_id)],
                m.happened_at,
                int(m.happened_at.utcoffset().total_seconds() / 60),
                m.ended_at,
                m.author.value if m.author else None,
                ident["id"] if ident else None,
                ident["person_id"] if ident else None,
                m.content,
                Jsonb(m.images),
                Jsonb(m.fields),
                location.lat if location else None,
                location.lon if location else None,
                location.accuracy_m if location else None,
                "source" if location else None,
                "no_track_point_within_tolerance"
                if kind == "life_event"
                else "not_applicable",
                m.access_label,
                digest,
                subject.start if subject else None,
                subject.end if subject else None,
                # A precision describes a bound; an open end has none.
                subject.start_precision if subject and subject.start else None,
                subject.end_precision if subject and subject.end else None,
                Jsonb(m.payload),
            )
        )
    with conn.cursor().copy("COPY incoming_envelopes FROM STDIN") as copy:
        for envelope in envelopes:
            copy.write_row(envelope)
    with conn.cursor().copy("COPY incoming FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)
    conn.execute(
        "UPDATE incoming i SET lat=t.lat,lon=t.lon,loc_accuracy_m=t.accuracy_m,loc_method='track',loc_reason=NULL FROM LATERAL (SELECT 1) dummy,tl.channels c,tl.location_track t WHERE c.id=i.channel_id AND c.record_kind='life_event' AND i.lat IS NULL AND t.id=(SELECT p.id FROM tl.location_track p WHERE NOT EXISTS(SELECT 1 FROM tl.retired_track r WHERE r.track_id=p.id) AND p.at BETWEEN i.happened_at-make_interval(secs=>c.location_tolerance_s) AND i.happened_at+make_interval(secs=>c.location_tolerance_s) ORDER BY abs(extract(epoch FROM p.at-i.happened_at)),p.accuracy_m NULLS LAST,p.id LIMIT 1)"
    )
    conn.execute("UPDATE incoming SET loc_reason=NULL WHERE lat IS NOT NULL")
    conn.execute("""CREATE TEMP TABLE thread_plan ON COMMIT DROP AS
      SELECT i.thread_id,coalesce(old.thread_no,0) AS base_no,
        NOT EXISTS(SELECT 1 FROM incoming e WHERE e.thread_id=i.thread_id AND e.old_id IS NOT NULL)
        AND NOT EXISTS(SELECT 1 FROM incoming e JOIN tl.messages prior ON prior.id=e.old_id WHERE prior.thread_id=i.thread_id)
        AND (old.happened_at IS NULL OR (old.happened_at,old.external_id)<(i.happened_at,i.external_id)) AS appendable
      FROM (SELECT DISTINCT ON(thread_id) thread_id,happened_at,external_id FROM incoming ORDER BY thread_id,happened_at,external_id) i
      LEFT JOIN LATERAL (SELECT happened_at,external_id,thread_no FROM tl.messages WHERE thread_id=i.thread_id AND superseded_by IS NULL ORDER BY happened_at DESC,external_id DESC LIMIT 1) old ON true""")
    cols = "channel_id,external_id,version,thread_id,happened_at,happened_offset_min,ended_at,author_handle,author_identifier_id,person_id,content,images,fields,lat,lon,loc_accuracy_m,loc_method,loc_reason,access_label,payload_hash,subject_from,subject_to,subject_from_precision,subject_to_precision"
    conn.execute(
        "CREATE TEMP TABLE inserted_messages(id bigint,channel_id bigint,external_id text,version int,happened_at timestamptz,loc_method text) ON COMMIT DROP"
    )
    conn.execute(
        f"WITH added AS (INSERT INTO tl.messages({cols}) SELECT {cols} FROM incoming ORDER BY seq RETURNING id,channel_id,external_id,version,happened_at,loc_method) INSERT INTO inserted_messages SELECT * FROM added"
    )
    # Temporary tables have no statistics: without them the planner takes the
    # joins below for one row and the thread numbering turns quadratic in the
    # number of new threads (minutes for one batch of 5,000 conversations).
    conn.execute("ANALYZE incoming,thread_plan,inserted_messages")
    conn.execute(
        "INSERT INTO tl.raw_payloads SELECT m.id,i.payload FROM incoming i JOIN inserted_messages m USING(channel_id,external_id,version)"
    )
    old_ids = [row[4] for row in rows if row[4] is not None]
    if old_ids:
        conn.execute(
            "UPDATE tl.messages old SET superseded_by=m.id FROM incoming i JOIN inserted_messages m USING(channel_id,external_id,version) WHERE old.id=i.old_id"
        )
        for side in ("from", "to"):
            conn.execute(
                f"UPDATE tl.links l SET {side}_id=m.id FROM incoming i JOIN inserted_messages m USING(channel_id,external_id,version) WHERE l.{side}_kind='message' AND l.{side}_id=i.old_id"
            )
    conn.execute(
        "INSERT INTO tl.message_envelopes(message_id,envelope) SELECT m.id,i.envelope FROM incoming_envelopes i JOIN inserted_messages m USING(channel_id,external_id,version) ON CONFLICT DO NOTHING"
    )
    affected = sorted(
        {r[5] for r in rows}
        | {
            r["thread_id"]
            for r in conn.execute(
                "SELECT DISTINCT thread_id FROM tl.messages WHERE id=ANY(%s)",
                ([row[4] for row in rows if row[4] is not None],),
            )
        }
    )
    for tid in affected:
        conn.execute("SELECT pg_advisory_xact_lock(%s,%s)", (991, tid))
    # Large historical threads defer repeated whole-thread maintenance until
    # observation. api.message_versions computes dense numbers for dirty threads.
    large = {
        r["thread_id"]
        for r in conn.execute(
            "SELECT thread_id FROM tl.dirty_threads WHERE thread_id=ANY(%s)",
            (affected,),
        )
    }
    candidates = [tid for tid in affected if tid not in large]
    if candidates:
        large.update(
            r["thread_id"]
            for r in conn.execute(
                "SELECT thread_id FROM tl.messages WHERE superseded_by IS NULL AND thread_id=ANY(%s) GROUP BY thread_id HAVING count(*)>256",
                (candidates,),
            )
        )
    eager = [tid for tid in affected if tid not in large]
    if large:
        conn.execute(
            "INSERT INTO tl.dirty_threads SELECT unnest(%s::bigint[]) ON CONFLICT DO NOTHING",
            (list(large),),
        )
    if eager:
        conn.execute(
            """WITH numbered AS (
              SELECT m.id,(p.base_no+row_number() OVER (PARTITION BY i.thread_id ORDER BY i.happened_at,i.external_id))::int no
              FROM incoming i JOIN inserted_messages m USING(channel_id,external_id,version) JOIN thread_plan p ON p.thread_id=i.thread_id AND p.appendable AND p.thread_id=ANY(%s)
              UNION ALL
              SELECT m.id,row_number() OVER (PARTITION BY m.thread_id ORDER BY m.happened_at,m.external_id)::int no
              FROM tl.messages m WHERE m.superseded_by IS NULL AND m.thread_id=ANY(%s)
              AND NOT EXISTS(SELECT 1 FROM thread_plan p WHERE p.thread_id=m.thread_id AND p.appendable)
            ) UPDATE tl.messages m SET thread_no=n.no FROM numbered n WHERE m.id=n.id AND m.thread_no IS DISTINCT FROM n.no""",
            (eager, eager),
        )
        conn.execute("SELECT tl.regroup_conversations(%s::bigint[])", (eager,))
    if conn.execute(
        "SELECT EXISTS(SELECT 1 FROM incoming WHERE loc_method='track') present"
    ).fetchone()["present"]:
        conn.execute("""INSERT INTO tl.message_locations(message_id,track_id)
          SELECT m.id,p.id FROM incoming i JOIN inserted_messages m USING(channel_id,external_id,version)
          JOIN tl.channels c ON c.id=m.channel_id
          CROSS JOIN LATERAL (SELECT p.id FROM tl.location_track p WHERE NOT EXISTS(SELECT 1 FROM tl.retired_track r WHERE r.track_id=p.id) AND p.at BETWEEN m.happened_at-make_interval(secs=>c.location_tolerance_s) AND m.happened_at+make_interval(secs=>c.location_tolerance_s) ORDER BY abs(extract(epoch FROM p.at-m.happened_at)),p.accuracy_m NULLS LAST,p.id LIMIT 1) p
          WHERE m.loc_method='track' ON CONFLICT(message_id) DO UPDATE SET track_id=EXCLUDED.track_id""")
    conn.execute("DROP TABLE incoming,thread_plan,incoming_envelopes,inserted_messages")
    _apply_side_effects(conn, effects, result)
    return result, deferred, resolved_keys


def _delivery(canonical, payload_hash, quality):
    return dict(
        external_id=canonical, last_payload_hash=payload_hash, identity_quality=quality
    )


def _resolve(conn, checked, result):
    """Resolve each validated item to its canonical record and
    decide whether it writes a version.

    Order: (a) the external id is a known alias -> its canonical record;
    (b) a current record exists under the external id -> that record, and an
    alias naming another existing record links the two with same_as;
    (c) an alias names an existing record -> the delivered id becomes an alias
    of it. Once a record has an alias, every delivery is compared with the
    last payload hash of its own delivered key (tl.message_deliveries), so
    alternating sources whose payload shapes differ do not flip versions.

    Returns (items to write [(message under its canonical id, channel,
    digest, current row)], deferred [(index, data)], resolved keys
    [(index, channel_id, canonical)], side effects for _apply_side_effects).
    """
    side = dict(aliases={}, deliveries={}, pairs=[])
    if not checked:
        return [], [], [], side
    cids = sorted({c["id"] for _, _, c in checked})
    keys = sorted({k for _, m, _ in checked for k in (m.external_id, *m.aliases)})
    alias_map = {
        (r["channel_id"], r["alias"]): r["external_id"]
        for r in conn.execute(
            "SELECT channel_id,alias,external_id FROM tl.message_aliases WHERE channel_id=ANY(%s) AND alias=ANY(%s)",
            (cids, keys),
        )
    }
    lookup = sorted(set(keys) | set(alias_map.values()))
    current = {
        (r["channel_id"], r["external_id"]): r
        for r in conn.execute(
            "SELECT id,channel_id,external_id,version,payload_hash,fields->>'identity_quality' AS quality FROM tl.messages WHERE superseded_by IS NULL AND channel_id=ANY(%s) AND external_id=ANY(%s)",
            (cids, lookup),
        )
    }
    # A record with an alias is versioned per delivered key, even when this
    # delivery names neither an alias nor carries aliases (the archive key).
    aliased_records = {
        (r["channel_id"], r["external_id"])
        for r in conn.execute(
            "SELECT DISTINCT channel_id,external_id FROM tl.message_aliases WHERE channel_id=ANY(%s) AND external_id=ANY(%s)",
            (cids, lookup),
        )
    }
    deliveries = {}
    if aliased_records or any(m.aliases for _, m, _ in checked):
        deliveries = {
            (r["channel_id"], r["delivered_key"]): r
            for r in conn.execute(
                "SELECT channel_id,delivered_key,external_id,last_payload_hash,identity_quality FROM tl.message_deliveries WHERE channel_id=ANY(%s) AND delivered_key=ANY(%s)",
                (cids, lookup),
            )
        }
    new_aliases = side["aliases"]
    writes, deferred, resolved = [], [], []
    touched = set()

    def target(cid, key):
        """The canonical external id an existing key names, or None."""
        found = alias_map.get((cid, key)) or new_aliases.get((cid, key))
        if found is None and (cid, key) in current:
            found = key
        return found

    for index, m, c in checked:
        cid, ext = c["id"], m.external_id
        own = {(cid, ext)} | {(cid, a) for a in m.aliases}
        known = alias_map.get((cid, ext)) or new_aliases.get((cid, ext))
        if own & touched or (known is not None and (cid, known) in touched):
            # Repeated keys inside a batch are applied in delivery order,
            # including edits: the next pass sees this pass's writes.
            deferred.append(
                (
                    index,
                    m.model_dump(mode="json")
                    | {"aliases": m.aliases, "expected_version": m.expected_version},
                )
            )
            continue
        status = pair = None
        if known is not None:
            canonical, status = known, "aliased"
        elif (cid, ext) in current:
            canonical = ext
            for a in m.aliases:
                other = target(cid, a)
                if other is not None and other != ext:
                    pair = other
                    break
        else:
            canonical = ext
            for a in m.aliases:
                other = target(cid, a)
                if other is not None:
                    canonical, status = other, "aliased"
                    break
        old = current.get((cid, canonical))
        touched |= own | {(cid, canonical)}
        if m.expected_version is not None:
            version = old["version"] if old else 0
            if version != m.expected_version:
                result["rejected"].append(
                    dict(
                        index=index,
                        external_id=ext,
                        error=f"version_conflict: expected version {m.expected_version}, current version {version}",
                        reason="version_conflict",
                        current_version=version,
                    )
                )
                continue
        register = [ext] if canonical != ext and (cid, ext) not in alias_map else []
        for a in m.aliases:
            if (
                a != canonical
                and a != pair
                and (cid, a) not in alias_map
                and (cid, a) not in new_aliases
                and (cid, a) not in current
            ):
                register.append(a)
        for a in register:
            new_aliases[(cid, a)] = canonical
            touched.add((cid, a))
        if register:
            aliased_records.add((cid, canonical))
        if status == "aliased":
            result["aliased"].append(
                dict(
                    index=index, status="aliased", external_id=ext, canonical=canonical
                )
            )
        if pair is not None:
            side["pairs"].append((index, c["name"], ext, pair))
        resolved.append((index, cid, canonical))
        message = (
            m if canonical == ext else m.model_copy(update={"external_id": canonical})
        )
        digest = payload_digest(m, canonical)
        quality = m.fields.get("identity_quality")
        quality = quality if isinstance(quality, str) else None
        if (cid, canonical) in aliased_records:
            write = old is None
            if old is not None:
                if (cid, canonical) not in deliveries:
                    # The canonical key's delivery starts from the current
                    # version when the record gets its first alias.
                    deliveries[(cid, canonical)] = side["deliveries"][
                        (cid, canonical)
                    ] = _delivery(canonical, old["payload_hash"], old["quality"])
                seen = deliveries.get((cid, ext))
                if seen is not None:
                    # A key already seen: a version only when its own payload
                    # changed since its last delivery.
                    write = (
                        seen["last_payload_hash"] != digest
                        and digest != old["payload_hash"]
                    )
                else:
                    # A key seen for the first time: a version only for an
                    # identity upgrade; otherwise the delivery is recorded.
                    write = digest != old["payload_hash"] and quality_rank(
                        quality
                    ) > quality_rank(old["quality"])
            deliveries[(cid, ext)] = side["deliveries"][(cid, ext)] = _delivery(
                canonical, digest, quality
            )
        else:
            write = old is None or old["payload_hash"] != digest
        if write:
            writes.append((message, c, digest, old))
        else:
            result["unchanged"] += 1
    return writes, deferred, resolved, side


def _apply_side_effects(conn, side, result):
    """Alias rows, per-key deliveries and same_as pair links of one pass."""
    if side["aliases"]:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO tl.message_aliases(channel_id,alias,external_id,origin) VALUES (%s,%s,%s,'source') ON CONFLICT DO NOTHING",
                [
                    (cid, alias, canonical)
                    for (cid, alias), canonical in side["aliases"].items()
                ],
            )
    if side["deliveries"]:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO tl.message_deliveries(channel_id,delivered_key,external_id,last_payload_hash,identity_quality,last_seen_at) VALUES (%s,%s,%s,%s,%s,now())"
                " ON CONFLICT(channel_id,delivered_key) DO UPDATE SET external_id=EXCLUDED.external_id,last_payload_hash=EXCLUDED.last_payload_hash,identity_quality=EXCLUDED.identity_quality,last_seen_at=now()",
                [
                    (
                        cid,
                        key,
                        d["external_id"],
                        d["last_payload_hash"],
                        d["identity_quality"],
                    )
                    for (cid, key), d in side["deliveries"].items()
                ],
            )
    for index, channel, ext, other in side["pairs"]:
        # Two records already exist for one message: keep both (archive ids
        # never change) and say they are the same; recall collapses the pair.
        row = add_link(
            conn,
            {
                "type": "same_as",
                "from": {"channel": channel, "external_id": ext},
                "to": {"channel": channel, "external_id": other},
                "method": "source",
            },
        )
        result["aliased"].append(
            dict(
                index=index,
                status="pair_linked",
                external_id=ext,
                canonical=other,
                link_id=row["id"],
            )
        )


def ingest(conn, batch, plugin_scope=None, *, resolve_identity=True, keys=None):
    """Ingest a batch: messages (set-oriented, per-item rejection), then its
    links with per-item results (a vocabulary refusal is reported per item;
    any other link error raises and rolls the batch back).

    `keys`, when a list, receives [(index, channel_id, canonical external id)]
    of every message item that was not rejected (for receipts)."""
    result = None
    pending = list(enumerate(batch.messages))
    resolved = {}
    while True:
        part, deferred, done = _ingest_pass(
            conn, batch, pending, plugin_scope, resolve_identity=resolve_identity
        )
        for key in done:
            resolved[key[0]] = key
        if result is None:
            result = part
            result["received"] = len(batch.messages)
        else:
            for key in ("inserted", "new_versions", "unchanged"):
                result[key] += part[key]
            result["rejected"].extend(part["rejected"])
            result["aliased"].extend(part["aliased"])
        if not deferred:
            break
        pending = deferred
    result["rejected"].sort(key=lambda r: r["index"])
    result["aliased"].sort(key=lambda r: (r["index"], r["status"]))
    result["link_results"] = link_batch(conn, batch.links)
    if keys is not None:
        keys.extend(resolved[i] for i in sorted(resolved))
    return result


def link_status(conn, data, row, created):
    """created | existing | folded | candidate for a link add_link returned."""
    if not created:
        return "existing"
    written = conn.execute(
        "SELECT tl.normalize_link_type(%s) AS t,(SELECT status FROM tl.link_types WHERE type=%s) AS status",
        (data["type"], row["type"]),
    ).fetchone()
    if written["t"] != row["type"]:
        return "folded"
    if written["status"] == "candidate":
        return "candidate"
    return "created"


def is_vocabulary_refusal(error):
    """The link-type gate refused an unregistered or retired type."""
    return (
        isinstance(error, psycopg.Error)
        and error.sqlstate == "23514"
        and "link type" in (error.diag.message_primary or "")
    )


def link_batch(conn, links, *, errors="raise"):
    """Write links one by one, each in its own savepoint; authorization is the
    caller's and runs before this. A vocabulary refusal is a per-item
    'refused'; any other error raises (errors='raise') or is a per-item
    'error' (errors='item'). Returns [{index, status, reason, link_id}]."""
    results = []
    for index, data in enumerate(links):
        try:
            with conn.transaction():
                row, created = add_link(conn, data, outcome=True)
                status = link_status(conn, data, row, created)
            results.append(
                dict(index=index, status=status, reason=None, link_id=row["id"])
            )
        except psycopg.Error as error:
            if is_vocabulary_refusal(error):
                reason = error.diag.message_primary
            elif errors == "item":
                reason = f"database operation rejected ({error.sqlstate})"
            else:
                raise
            results.append(
                dict(
                    index=index,
                    status="refused" if is_vocabulary_refusal(error) else "error",
                    reason=reason,
                    link_id=None,
                )
            )
        except (ValueError, TypeError, KeyError) as error:
            if errors != "item":
                raise
            reason = f"missing {error}" if isinstance(error, KeyError) else str(error)
            results.append(
                dict(index=index, status="error", reason=reason, link_id=None)
            )
    return results


def resolve_key(conn, channel, external_id):
    """The current message id named by {channel, external_id}, through
    tl.message_aliases when the id is an alias; None when there is none."""
    row = conn.execute(
        "SELECT m.id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s AND m.external_id=%s AND m.superseded_by IS NULL",
        (channel, external_id),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT m.id FROM tl.message_aliases a JOIN tl.channels c ON c.id=a.channel_id JOIN tl.messages m ON m.channel_id=a.channel_id AND m.external_id=a.external_id AND m.superseded_by IS NULL WHERE c.name=%s AND a.alias=%s",
            (channel, external_id),
        ).fetchone()
    return row["id"] if row else None


def endpoint(conn, end):
    if "id" in end:
        kind, ident = end["kind"], end["id"]
        if kind == "message":
            row = conn.execute("SELECT api.current_message(%s) id", (ident,)).fetchone()
            ident = row["id"]
        elif kind in ("entity", "person"):
            table = "entities" if kind == "entity" else "people"
            while True:
                row = conn.execute(
                    f"SELECT merged_into FROM tl.{table} WHERE id=%s", (ident,)
                ).fetchone()
                if not row:
                    raise ValueError("link endpoint not found")
                if row["merged_into"] is None:
                    break
                ident = row["merged_into"]
        if ident is None:
            raise ValueError("link endpoint not found")
        return kind, ident
    if "channel" in end:
        # An alias of a record resolves to the record.
        ident = resolve_key(conn, end["channel"], end["external_id"])
        if ident is None:
            # HTTP 400 with code 'link_message_missing'. Plugins drop links to
            # messages they never delivered by that code; older plugins match
            # the detail text, so it stays as it is.
            raise CodedError("link_message_missing", "link message does not exist")
        return "message", ident
    row = conn.execute(
        "INSERT INTO tl.entities(kind,external_ref,name,is_placeholder) VALUES (%s,%s,%s,true) ON CONFLICT(kind,external_ref) DO UPDATE SET external_ref=EXCLUDED.external_ref RETURNING id",
        (end["kind"], end["external_ref"], end.get("name")),
    ).fetchone()
    return endpoint(conn, {"kind": "entity", "id": row["id"]})


def add_link(conn, data, *, outcome=False):
    """Write a link, or return the equal active link (a replay). With
    outcome=True returns (row, created)."""
    evidence = data.get("evidence", {})
    method = data.get("method", "source")
    if method == "model" and not (
        isinstance(evidence, dict)
        and isinstance(evidence.get("message_ids"), list)
        and evidence["message_ids"]
    ):
        raise ValueError("model links require a non-empty evidence.message_ids")
    valid = validity(data)
    fk, fi = endpoint(conn, data["from"])
    tk, ti = endpoint(conn, data["to"])
    # The registry folds synonyms and normalises raw names on insert (trigger
    # link_type_gate), so the replay lookup and the lock use the canonical
    # type: replaying a link written under a synonym finds the folded link.
    link_type = conn.execute(
        "SELECT coalesce(tl.resolve_link_type(%(t)s),tl.normalize_link_type(%(t)s)) AS t",
        {"t": data["type"]},
    ).fetchone()["t"]
    # An acknowledged source link can be replayed after a cursor-write crash.
    # Serialize equivalent writes and return the existing active assertion.
    # Validity is part of the assertion: a changed interval is a new link.
    values = (
        link_type,
        fk,
        fi,
        tk,
        ti,
        data.get("confidence", 1),
        method,
        Jsonb(evidence),
        data.get("job"),
        data.get("job_version"),
        *(valid.get(key) for key in VALIDITY),
    )
    lock_key = json.dumps([link_type, fk, fi, tk, ti], separators=(",", ":"))
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (lock_key,))
    existing = conn.execute(
        "SELECT * FROM tl.links WHERE status='active' AND type=%s AND from_kind=%s AND from_id=%s AND to_kind=%s AND to_id=%s AND confidence=%s AND method=%s AND evidence=%s AND job IS NOT DISTINCT FROM %s AND job_version IS NOT DISTINCT FROM %s"
        " AND valid_from IS NOT DISTINCT FROM %s::timestamptz AND valid_to IS NOT DISTINCT FROM %s::timestamptz AND valid_from_precision IS NOT DISTINCT FROM %s::text AND valid_to_precision IS NOT DISTINCT FROM %s::text ORDER BY id LIMIT 1",
        values,
    ).fetchone()
    if existing:
        return (existing, False) if outcome else existing
    # The versions named at creation stay pinned; from_id/to_id follow new
    # versions of the message.
    row = conn.execute(
        "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id,confidence,method,evidence,job,job_version,valid_from,valid_to,valid_from_precision,valid_to_precision,from_version_id,to_version_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        # The raw type is inserted: the trigger folds it and keeps it in
        # type_as_written when folding or normalisation changed it.
        (data["type"],)
        + values[1:]
        + (
            fi if fk == "message" else None,
            ti if tk == "message" else None,
        ),
    ).fetchone()
    return (row, True) if outcome else row


def reconcile_scheme(conn, scheme, actor):
    """Canonicalize auto-created identifiers when a registry arrives later."""
    from .models import Author

    schemes = {(scheme["system"], scheme["kind"]): scheme}
    rows = conn.execute(
        "SELECT * FROM tl.identifiers WHERE system=%s AND kind=%s ORDER BY id FOR UPDATE",
        (scheme["system"], scheme["kind"]),
    ).fetchall()
    for row in rows:
        system, kind, value, norm = normalize_author(
            conn,
            Author(system=row["system"], kind=row["kind"], value=row["value"]),
            schemes,
        )
        if (row["system"], row["kind"], row["value_norm"]) == (system, kind, norm):
            continue
        if row["asserted_by"] == "owner" and actor != "owner":
            raise ValueError("owner assertion is protected")
        other = conn.execute(
            "SELECT * FROM tl.identifiers WHERE system=%s AND kind=%s AND value_norm=%s AND id<>%s FOR UPDATE",
            (system, kind, norm, row["id"]),
        ).fetchone()
        if other:
            if (
                other["person_id"] is not None
                and row["person_id"] is not None
                and other["person_id"] != row["person_id"]
            ):
                raise ValueError("normalization collides with a different person")
            if other["asserted_by"] == "owner" and actor != "owner":
                raise ValueError("owner assertion is protected")
            if row["asserted_by"] is not None:
                raise ValueError(
                    "normalization of asserted duplicates requires owner reconciliation"
                )
            conn.execute(
                "UPDATE tl.messages SET author_identifier_id=%s,person_id=%s WHERE author_identifier_id=%s",
                (other["id"], other["person_id"], row["id"]),
            )
            conn.execute("DELETE FROM tl.identifiers WHERE id=%s", (row["id"],))
        else:
            conn.execute(
                "UPDATE tl.identifiers SET system=%s,kind=%s,value=%s,value_norm=%s WHERE id=%s",
                (system, kind, value, norm, row["id"]),
            )
