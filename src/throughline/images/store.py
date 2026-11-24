"""Database side of images: assets, message links, faces, vectors, depicts.

Every function takes an open psycopg connection (throughline.db.connect() or
connect("app")) and runs inside the caller's transaction; nothing commits.

    with connect() as conn:
        asset_id = store.upsert_asset(conn, sha256=digest, origin="beeper", mime="image/jpeg")
        store.link_message(conn, channel="chat.discord", external_id="123", asset_id=asset_id)
        store.set_asset(conn, asset_id, immich_id=uuid, status="uploaded",
                        merge_enrich={"upload": {"v": 1}})

Stages and tl.assets.enrich: a stage is done for an asset once enrich has its
key ('pull', 'nsfw', 'tags', 'gemini'; any value, an {"error": ...} too),
except nsfw: an {"error": ...} without "gave_up" is retried (NudeNet errors
are counted in enrich.nsfw_error and given up after 3 attempts). Remove the
key to run the stage again. assets_needing() lists the assets a stage still
has to see.
"""

import json
import math
import re

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

KINDS = ("photo", "screenshot", "document", "artwork", "video", "other")
ROLES = ("attachment", "inline", "self", "preview")
# Tags the tags stage keeps when it rewrites an asset's tags.
STICKY_TAGS = ("shared-post",)
SHARED_POST_TAG = "shared-post"
STATUSES = ("pending", "uploaded", "enriched", "failed", "missing")
STAGES = ("upload", "pull", "nsfw", "tags", "gemini")
NSFW_THRESHOLD = 0.6  # the default; tl.image_settings.nsfw_threshold (0065) decides
OCR_INLINE = 300
DEPICTS_JOB = "images.depicts"
DEPICTS_JOB_VERSION = "1"
SIGLIP_MODEL = "siglip2-so400m-384"
GEMINI_MODEL = "gemini-embedding-2"

SET_COLUMNS = (
    "immich_id",
    "kind",
    "tags",
    "ocr_text",
    "caption",
    "taken_at",
    "lat",
    "lon",
    "place",
    "nsfw",
    "nsfw_labels",
    "status",
    "duration_s",
    "width",
    "height",
    "mime",
    "bytes",
    "original_name",
    "loc_method",
    "loc_accuracy_m",
)
# Columns added by migration 0065 (set only once it is applied).
_COLUMNS_0065 = ("loc_method", "loc_accuracy_m")
_JSON_COLUMNS = ("place", "nsfw_labels")
# Columns of an asset row returned to the pipeline (tsv is internal).
_ROW = (
    "id,sha256,immich_id,mime,bytes,width,height,duration_s,kind,tags,ocr_text,caption,"
    "taken_at,lat,lon,place,nsfw,nsfw_labels,origin,original_name,status,enrich,created_at,updated_at"
)

_ensured_indexes = set()
_has_0065 = {}


def _key(conn):
    info = getattr(conn, "info", None)
    return (getattr(info, "host", None), getattr(info, "port", None), getattr(info, "dbname", None))


def has_0065(conn):
    """Whether migration 0065 is applied (cached per database for the process)."""
    key = _key(conn)
    if not _has_0065.get(key):
        row = (
            conn.cursor(row_factory=dict_row)
            .execute(
                "SELECT to_regclass('tl.image_settings') IS NOT NULL AND EXISTS(SELECT 1 FROM pg_attribute"
                " WHERE attrelid='tl.assets'::regclass AND attname='loc_method' AND NOT attisdropped) AS ok"
            )
            .fetchone()
        )
        _has_0065[key] = bool(row and row["ok"])
    return _has_0065[key]


def _row(conn):
    return _ROW + (",loc_method,loc_accuracy_m" if has_0065(conn) else "")


def nsfw_threshold(conn):
    """The current threshold (tl.image_settings), 0.6 before 0065."""
    if not has_0065(conn):
        return NSFW_THRESHOLD
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT nsfw_threshold FROM tl.image_settings WHERE one"
    ).fetchone()
    return float(row["nsfw_threshold"]) if row else NSFW_THRESHOLD


def _cur(conn):
    return conn.cursor(row_factory=dict_row)


def normalize_tag(tag):
    """Lower case, every run of other characters becomes '-': 'Sunset Beach' -> 'sunset-beach'."""
    return re.sub(r"[^a-z0-9]+", "-", str(tag or "").lower()).strip("-")


def normalize_tags(tags):
    out = []
    for tag in tags or ():
        value = normalize_tag(tag)
        if value and value not in out:
            out.append(value)
    return out


def is_nsfw(nsfw, tags=(), threshold=NSFW_THRESHOLD):
    if nsfw is not None:
        return float(nsfw) >= threshold
    return "nsfw" in (tags or ())


def asset_safe(row, threshold=NSFW_THRESHOLD, unscanned="hide"):
    """tl.asset_clean in Python (migration 0065): a scored asset is safe
    below the threshold (its score decides, whatever its tags say, so a
    changed threshold applies at once); an unscored one is unsafe when
    tagged nsfw, else safe when it is a video (NudeNet is image-only), when
    unscanned is 'show', or when its nsfw stage completed. An unscored image,
    a failed score and a given-up score are unsafe under unscanned='hide'."""
    row = row or {}
    nsfw = row.get("nsfw")
    if nsfw is not None:
        return float(nsfw) < float(threshold)
    if "nsfw" in (row.get("tags") or ()):
        return False
    if row.get("kind") == "video" or unscanned == "show":
        return True
    enrich = row.get("enrich") or {}
    if isinstance(enrich, str):
        enrich = json.loads(enrich)
    stage = enrich.get("nsfw")
    return isinstance(stage, dict) and "error" not in stage


# --- assets ---------------------------------------------------------------------


def upsert_asset(
    conn,
    *,
    sha256,
    origin,
    mime=None,
    bytes=None,
    width=None,
    height=None,
    original_name=None,
):
    """The id of the asset with these bytes, created when new. An existing
    asset keeps its origin (the first source) and gains only the facts it
    did not have yet."""
    if not sha256 or not origin:
        raise ValueError("sha256 and origin are required")
    digest = str(sha256).strip()
    if re.fullmatch(r"[0-9A-Fa-f]{64}", digest):
        digest = digest.lower()
    row = (
        _cur(conn)
        .execute(
            "INSERT INTO tl.assets(sha256,origin,mime,bytes,width,height,original_name)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (sha256) DO UPDATE SET"
            " mime=coalesce(tl.assets.mime,EXCLUDED.mime),"
            " bytes=coalesce(tl.assets.bytes,EXCLUDED.bytes),"
            " width=coalesce(tl.assets.width,EXCLUDED.width),"
            " height=coalesce(tl.assets.height,EXCLUDED.height),"
            " original_name=coalesce(tl.assets.original_name,EXCLUDED.original_name)"
            " RETURNING id",
            (digest, origin, mime, bytes, width, height, original_name),
        )
        .fetchone()
    )
    return row["id"]


def set_asset(conn, asset_id, merge_enrich=None, drop_enrich=None, **cols):
    """Set any of SET_COLUMNS; merge_enrich (a dict) is merged into enrich at
    the top level, after the keys in drop_enrich (a list) are removed. tags
    are normalised; an nsfw score at or above the threshold (tl.image_settings,
    0.6 before 0065) adds the tag 'nsfw'. Returns the updated row, or None
    for an unknown id."""
    unknown = set(cols) - set(SET_COLUMNS)
    if unknown:
        raise ValueError("unknown asset columns: " + ", ".join(sorted(unknown)))
    if "kind" in cols and cols["kind"] is not None and cols["kind"] not in KINDS:
        raise ValueError("kind must be one of " + ", ".join(KINDS))
    if "status" in cols and cols["status"] not in STATUSES:
        raise ValueError("status must be one of " + ", ".join(STATUSES))
    if "tags" in cols:
        cols["tags"] = normalize_tags(cols["tags"])
    if "immich_id" in cols and cols["immich_id"] is not None:
        cols["immich_id"] = str(cols["immich_id"])
    if "loc_method" in cols and cols["loc_method"] not in (None, "exif", "track"):
        raise ValueError("loc_method must be exif, track or None")
    if not has_0065(conn):
        for name in _COLUMNS_0065:
            cols.pop(name, None)
    elif "lat" in cols and "loc_method" not in cols:
        # A location set without its provenance is the file's own (EXIF).
        cols["loc_method"] = "exif" if cols["lat"] is not None else None
        if cols["lat"] is None or "loc_accuracy_m" not in cols:
            cols["loc_accuracy_m"] = None
    sets, values = [], []
    for name in SET_COLUMNS:
        if name not in cols:
            continue
        value = cols[name]
        if name in _JSON_COLUMNS and value is not None:
            value = Jsonb(value)
        sets.append(f"{name}=%s")
        values.append(value)
    nsfw = cols.get("nsfw")
    if nsfw is not None and float(nsfw) >= nsfw_threshold(conn):
        if "tags" in cols:
            if "nsfw" not in cols["tags"]:
                values[sets.index("tags=%s")] = cols["tags"] + ["nsfw"]
        else:
            sets.append(
                "tags=CASE WHEN 'nsfw'=ANY(tags) THEN tags ELSE array_append(tags,'nsfw') END"
            )
    if merge_enrich is not None and not isinstance(merge_enrich, dict):
        raise ValueError("merge_enrich must be a dict")
    drop = [str(k) for k in drop_enrich or ()]
    if merge_enrich or drop:
        sets.append("enrich=(enrich-%s::text[])||%s")
        values.extend([drop, Jsonb(merge_enrich or {})])
    if not sets:
        sets.append("updated_at=now()")
    return (
        _cur(conn)
        .execute(
            f"UPDATE tl.assets SET {','.join(sets)} WHERE id=%s RETURNING {_row(conn)}",
            (*values, asset_id),
        )
        .fetchone()
    )


def get_asset(conn, asset_id):
    return (
        _cur(conn)
        .execute(f"SELECT {_row(conn)} FROM tl.assets WHERE id=%s", (asset_id,))
        .fetchone()
    )


def assets_needing(conn, stage, limit=100):
    """Assets a pipeline stage still has to see, oldest first.

    upload: pending and not in Immich yet. pull: in Immich, enrich has no
    'pull'. nsfw, tags, gemini: pulled, enrich has no key of the stage (nsfw
    also: an error not given up yet). Assets marked missing are never listed."""
    if stage not in STAGES:
        raise ValueError("stage must be one of " + ", ".join(STAGES))
    if stage == "upload":
        where = "status='pending' AND immich_id IS NULL"
        params = ()
    elif stage == "pull":
        where = "immich_id IS NOT NULL AND status<>'missing' AND NOT enrich ? 'pull'"
        params = ()
    elif stage == "nsfw":
        where = (
            "immich_id IS NOT NULL AND status<>'missing' AND enrich ? 'pull' AND (NOT enrich ? 'nsfw'"
            " OR (jsonb_typeof(enrich->'nsfw')='object' AND enrich->'nsfw' ? 'error'"
            " AND NOT enrich->'nsfw' ? 'gave_up'))"
        )
        params = ()
    else:
        where = "immich_id IS NOT NULL AND status<>'missing' AND enrich ? 'pull' AND NOT enrich ? %s"
        params = (stage,)
    return (
        _cur(conn)
        .execute(
            f"SELECT {_row(conn)} FROM tl.assets WHERE {where} ORDER BY id LIMIT %s",
            (*params, max(0, int(limit))),
        )
        .fetchall()
    )


# --- message links ------------------------------------------------------------------


def link_message(
    conn,
    *,
    channel,
    external_id,
    asset_id,
    ord=0,
    role="attachment",
    source_ref=None,
):
    """Attach an asset to a message record (channel name, external id). The
    link survives new versions of the message. An asset without taken_at
    takes the message's time. Depicts links of the message are refreshed
    (a 'preview' link, the cover of a shared post, never names anyone).
    Before migration 0065 a 'preview' link is stored as 'attachment' (its
    source_ref keeps shared_url; reclassify-previews fixes the role)."""
    if role not in ROLES:
        raise ValueError("role must be one of " + ", ".join(ROLES))
    if role == "preview" and not has_0065(conn):
        role = "attachment"
    cur = _cur(conn)
    found = cur.execute("SELECT id FROM tl.channels WHERE name=%s", (channel,)).fetchone()
    if not found:
        raise ValueError(f"unknown channel: {channel}")
    channel_id = found["id"]
    cur.execute(
        "INSERT INTO tl.message_assets(channel_id,external_id,asset_id,ord,role,source_ref)"
        " VALUES (%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (channel_id,external_id,asset_id) DO UPDATE SET"
        " ord=EXCLUDED.ord,role=EXCLUDED.role,source_ref=EXCLUDED.source_ref",
        (channel_id, str(external_id), asset_id, int(ord), role, Jsonb(source_ref or {})),
    )
    cur.execute(
        "UPDATE tl.assets a SET taken_at=m.happened_at FROM tl.messages m"
        " WHERE a.id=%s AND a.taken_at IS NULL AND m.channel_id=%s AND m.external_id=%s AND m.superseded_by IS NULL",
        (asset_id, channel_id, str(external_id)),
    )
    _refresh(conn, _message_ids_of_keys(conn, [(channel_id, str(external_id))]))


# --- faces and depicts ------------------------------------------------------------------


def _face_identifier(cur, person_uuid, name):
    """(identifier id) of an Immich person; a new one with a name that is the
    display name of exactly one active person is linked to that person as
    suspected (a confirmation names every face of the cluster)."""
    value = str(person_uuid).strip()
    # Suspected until someone confirms the cluster: a face never names a
    # person by itself (tl.identifiers defaults to confirmed).
    row = cur.execute(
        "INSERT INTO tl.identifiers(system,kind,value,value_norm,label,asserted_by,status)"
        " VALUES ('immich','face',%s,%s,%s,'images','suspected')"
        " ON CONFLICT (system,kind,value_norm) DO UPDATE SET label=coalesce(EXCLUDED.label,tl.identifiers.label)"
        " RETURNING id,person_id",
        (value, value.lower(), name or None),
    ).fetchone()
    if row["person_id"] is None and name and str(name).strip():
        people = cur.execute(
            "SELECT id FROM tl.people WHERE merged_into IS NULL AND coalesce(status,'active')<>'ignored'"
            " AND lower(display_name)=lower(%s) LIMIT 2",
            (str(name).strip(),),
        ).fetchall()
        if len(people) == 1:
            cur.execute(
                "UPDATE tl.identifiers SET person_id=%s,status='suspected',asserted_by='images'"
                " WHERE id=%s AND person_id IS NULL",
                (people[0]["id"], row["id"]),
            )
            cur.execute(
                "INSERT INTO tl.identity_events(operation,asserted_by,detail) VALUES ('suspect','images',%s)",
                (
                    Jsonb(
                        dict(
                            identifier_id=row["id"],
                            person_id=people[0]["id"],
                            reason="immich name matches display name",
                        )
                    ),
                ),
            )
    return row["id"]


def replace_faces(conn, asset_id, faces):
    """Rewrite the faces of an asset from Immich. faces: dicts with
    immich_face_id, immich_person_id (None for an unassigned face), bbox
    ({x1,y1,x2,y2,w,h} in image pixels), score and optionally name (the
    Immich person name). Refreshes the asset's search text and the depicts
    links of its messages. Returns the number of faces stored."""
    cur = _cur(conn)
    identifiers = {}
    rows = []
    for face in faces or ():
        person = face.get("immich_person_id")
        identifier = None
        if person:
            key = str(person).lower()
            if key not in identifiers:
                identifiers[key] = _face_identifier(cur, person, face.get("name"))
            identifier = identifiers[key]
        bbox = face.get("bbox")
        if not isinstance(bbox, dict):
            raise ValueError("face bbox must be an object")
        rows.append(
            (
                asset_id,
                str(face["immich_face_id"]) if face.get("immich_face_id") else None,
                identifier,
                Jsonb(bbox),
                face.get("score"),
            )
        )
    cur.execute("DELETE FROM tl.asset_faces WHERE asset_id=%s", (asset_id,))
    for row in rows:
        cur.execute(
            "INSERT INTO tl.asset_faces(asset_id,immich_face_id,identifier_id,bbox,score) VALUES (%s,%s,%s,%s,%s)"
            " ON CONFLICT (immich_face_id) DO UPDATE SET asset_id=EXCLUDED.asset_id,"
            " identifier_id=EXCLUDED.identifier_id,bbox=EXCLUDED.bbox,score=EXCLUDED.score",
            row,
        )
    refresh_depicts(conn, asset_id)
    return len(rows)


def _message_ids_of_keys(conn, keys):
    if not keys:
        return []
    return [
        r["id"]
        for r in _cur(conn).execute(
            "SELECT m.id FROM tl.messages m JOIN unnest(%s::bigint[],%s::text[]) k(channel_id,external_id)"
            " ON m.channel_id=k.channel_id AND m.external_id=k.external_id WHERE m.superseded_by IS NULL",
            ([k[0] for k in keys], [k[1] for k in keys]),
        )
    ]


def refresh_depicts(conn, asset_id=None):
    """Bring the depicts links (message -> person) in line with the confirmed
    faces, for the messages of one asset, or for every message with images
    (asset_id None: run after identity changes). Also recomputes the search
    text of the affected assets (people names). Returns {added, removed,
    updated}."""
    cur = _cur(conn)
    if asset_id is None:
        cur.execute(
            "UPDATE tl.assets SET updated_at=now() WHERE id IN (SELECT DISTINCT asset_id FROM tl.asset_faces)"
        )
        ids = [
            r["id"]
            for r in cur.execute(
                "SELECT m.id FROM tl.message_assets ma JOIN tl.messages m ON m.channel_id=ma.channel_id"
                " AND m.external_id=ma.external_id AND m.superseded_by IS NULL"
                " UNION SELECT l.from_id FROM tl.links l WHERE l.type='depicts' AND l.job=%s"
                " AND l.from_kind='message' AND l.status='active'",
                (DEPICTS_JOB,),
            )
        ]
    else:
        cur.execute("UPDATE tl.assets SET updated_at=now() WHERE id=%s", (asset_id,))
        ids = [
            r["id"]
            for r in cur.execute(
                "SELECT m.id FROM tl.message_assets ma JOIN tl.messages m ON m.channel_id=ma.channel_id"
                " AND m.external_id=ma.external_id AND m.superseded_by IS NULL WHERE ma.asset_id=%s"
                " UNION SELECT l.from_id FROM tl.links l WHERE l.type='depicts' AND l.job=%s"
                " AND l.from_kind='message' AND l.status='active' AND l.evidence->'asset_ids' @> to_jsonb(ARRAY[%s::bigint])",
                (asset_id, DEPICTS_JOB, asset_id),
            )
        ]
    return _refresh(conn, ids)


def _refresh(conn, message_ids):
    counts = dict(added=0, removed=0, updated=0)
    ids = sorted({int(i) for i in message_ids or ()})
    if not ids:
        return counts
    cur = _cur(conn)
    wanted = {
        (r["message_id"], r["person_id"]): sorted(r["asset_ids"])
        for r in cur.execute(
            "SELECT m.id AS message_id,p.id AS person_id,array_agg(DISTINCT ma.asset_id) AS asset_ids"
            " FROM tl.messages m"
            " JOIN tl.message_assets ma ON ma.channel_id=m.channel_id AND ma.external_id=m.external_id"
            " AND ma.role<>'preview'"
            " JOIN tl.asset_faces f ON f.asset_id=ma.asset_id"
            " JOIN tl.identifiers i ON i.id=f.identifier_id AND i.status='confirmed'"
            " JOIN tl.people p ON p.id=i.person_id AND p.merged_into IS NULL"
            " WHERE m.id=ANY(%s) GROUP BY m.id,p.id",
            (ids,),
        )
    }
    existing = {}
    for r in cur.execute(
        "SELECT id,from_id,to_id,evidence FROM tl.links WHERE type='depicts' AND job=%s"
        " AND from_kind='message' AND to_kind='person' AND status='active' AND from_id=ANY(%s)"
        " ORDER BY id",
        (DEPICTS_JOB, ids),
    ).fetchall():
        key = (r["from_id"], r["to_id"])
        if key in existing or key not in wanted:
            cur.execute("DELETE FROM tl.links WHERE id=%s", (r["id"],))
            counts["removed"] += 1
            continue
        existing[key] = r
    for key, asset_ids in sorted(wanted.items()):
        evidence = {"asset_ids": asset_ids}
        row = existing.get(key)
        if row is None:
            cur.execute(
                "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id,confidence,method,evidence,job,job_version)"
                " VALUES ('depicts','message',%s,'person',%s,1,'rule',%s,%s,%s)",
                (key[0], key[1], Jsonb(evidence), DEPICTS_JOB, DEPICTS_JOB_VERSION),
            )
            counts["added"] += 1
        elif (row["evidence"] or {}).get("asset_ids") != asset_ids:
            cur.execute(
                "UPDATE tl.links SET evidence=%s WHERE id=%s", (Jsonb(evidence), row["id"])
            )
            counts["updated"] += 1
    return counts


# --- vectors -----------------------------------------------------------------------------


def put_vector(conn, asset_id, model, vector):
    """Store (or replace) the asset's vector of one model, and make sure the
    model has its nearest-neighbour index at this size."""
    values = [float(x) for x in vector]
    if not values or not all(math.isfinite(x) for x in values):
        raise ValueError("vector must be a non-empty list of finite numbers")
    text = "[" + ",".join(repr(x) for x in values) + "]"
    cur = _cur(conn)
    cur.execute(
        "INSERT INTO tl.asset_vectors(asset_id,model,embedding) VALUES (%s,%s,%s::vector)"
        " ON CONFLICT (asset_id,model) DO UPDATE SET embedding=EXCLUDED.embedding,created_at=now()",
        (asset_id, model, text),
    )
    key = (conn.info.dbname, model, len(values))
    if key not in _ensured_indexes:
        cur.execute("SELECT tl.ensure_asset_vector_index(%s,%s)", key[1:])
        _ensured_indexes.add(key)


# --- text rendering ------------------------------------------------------------------------


def _place_text(place):
    if isinstance(place, str):
        try:
            place = json.loads(place)
        except ValueError:
            return place.strip() or None
    if not isinstance(place, dict):
        return None
    parts = []
    for key in ("city", "state", "country"):
        value = place.get(key)
        if isinstance(value, str) and value.strip() and value.strip() not in parts:
            parts.append(value.strip())
    return ", ".join(parts) or None


def render_text(asset_row, people_names=()):
    """The short text block of an asset (docs/images.md):

        [photo · beach, ocean, sunset · Galveston, Texas, US · with Alex Kim, Sam Lee]
        caption: A pier at sunset
        text: "WELCOME TO GALVESTON"

    Only the parts that exist are printed; an unclassified asset is
    'image'; a place inferred from the location track (loc_method 'track')
    reads 'near <place> (from location history)'. OCR is cut to 300 characters (whitespace collapsed)."""
    row = asset_row or {}
    header = [row.get("kind") or "image"]
    tags = [t for t in (row.get("tags") or []) if t]
    if tags:
        header.append(", ".join(tags))
    place = _place_text(row.get("place"))
    if place:
        if row.get("loc_method") == "track":
            place = f"near {place} (from location history)"
        header.append(place)
    names = [n for n in dict.fromkeys(people_names or ()) if n]
    if names:
        header.append("with " + ", ".join(names))
    lines = ["[" + " · ".join(header) + "]"]
    caption = " ".join(str(row.get("caption") or "").split())
    if caption:
        lines.append("caption: " + caption)
    ocr = " ".join(str(row.get("ocr_text") or row.get("ocr") or "").split())
    if ocr:
        if len(ocr) > OCR_INLINE:
            ocr = ocr[: OCR_INLINE - 1].rstrip() + "…"
        lines.append(f'text: "{ocr}"')
    return "\n".join(lines)


def people_names(conn, asset_id):
    """Display names of the confirmed people in an asset, in name order."""
    return [
        r["display_name"]
        for r in _cur(conn).execute(
            "SELECT DISTINCT p.display_name FROM tl.asset_faces f JOIN tl.identifiers i ON i.id=f.identifier_id"
            " JOIN tl.people p ON p.id=i.person_id WHERE f.asset_id=%s AND i.status='confirmed'"
            " AND p.merged_into IS NULL AND p.display_name IS NOT NULL ORDER BY 1",
            (asset_id,),
        )
    ]
