"""The image pipeline: discover -> fetch -> upload -> pull -> locate -> nsfw -> tags -> gemini.

Every stage is idempotent, bounded by a per-run limit, commits per asset (a
stopped run resumes where it stopped) and returns a Counter of what it did.
State lives in tl.assets (status, enrich keys: see store.assets_needing) and
in a spool directory holding the bytes of assets not yet in Immich.

    p = Pipeline(conn, Settings.from_env())
    p.backfill("t3code", limit=50)          # fetch + store + link (dry_run=True counts only)
    p.upload(limit=50); p.pull(limit=50); p.locate(limit=50)
    p.nsfw(limit=50); p.tags(limit=50); p.gemini(limit=50)
    p.reclassify_previews(dry_run=True)       # shared-post covers -> role preview
    p.native(limit=50); p.native_messages()  # Immich-native assets -> photos.immich
    p.sync()                                 # all of the above with default limits

Collaborators (Immich, Immich ML, Immich DB, NudeNet, Gemini, resolvers, the
store) are injected so the tests run without a network or a database.
"""

import hashlib
import json
import logging
import math
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import gemini as gemini_door
from . import nsfw as nsfw_mod
from . import shared, sniff, tagging
from . import user_settings
from .store import SHARED_POST_TAG, STICKY_TAGS, _place_text, asset_safe
from .sources import SOURCES, Candidate, module as source_module

LOG = logging.getLogger("throughline.images")
SIGLIP = "siglip2-so400m-384"
GEMINI = "gemini-embedding-2"
NATIVE_CHANNEL = "photos.immich"
CAPTURE_PREFIX = "tl-capture-"
PLUGIN = "images"
PULL_VERSION = 1
# Version of the Immich description format (enrich.describe_v). 2: source,
# thread title and author only, never message text.
DESCRIBE_VERSION = 2
NSFW_ATTEMPTS = 3
CHANNEL_LABELS = {
    "chat.discord": "Discord",
    "chat.instagram": "Instagram",
    "chat.sms": "SMS",
    "chat.reddit": "Reddit",
    "chat.snapchat": "Snapchat",
    "email.message": "Email",
    "hermes.messages": "Hermes",
    "hermes.automation": "Hermes",
    "t3code.messages": "T3 Code",
    "capture.screens": "Screen capture",
}
KIND_ALBUMS = {"screenshot": "Screenshots", "document": "Documents", "artwork": "Artwork"}
MEME_ALBUM = "Memes"
SHARED_ALBUM = "Shared posts"
# Appended to the Immich description of an asset placed by the location track.
LOCATED_SUFFIX = " · location from history"
LOCATE_VERSION = 1
# Read Immich's reverse geocoding back this long after pushing a location;
# give up on a place after GEOCODE_PATIENCE.
GEOCODE_DELAY = timedelta(minutes=2)
GEOCODE_PATIENCE = timedelta(hours=6)
# Pull anyway, with whatever Immich has, once this long has passed since upload.
PULL_PATIENCE = timedelta(hours=6)
SESSION_GAP = timedelta(hours=2)
SESSION_KM = 3.0


def label_of(channel):
    if channel in CHANNEL_LABELS:
        return CHANNEL_LABELS[channel]
    return channel.split(".", 1)[0].capitalize() if channel else None


def describe(links):
    """Short Immich description of an asset's first linked message: source,
    thread title and author ("Discord · #general · from Alex Kim"). Never
    message text: Immich is outside the asker gate."""
    if not links:
        return None
    first = links[0]
    parts = [first.get("label") or label_of(first.get("channel"))]
    if first.get("thread"):
        parts.append(" ".join(str(first["thread"]).split())[:80])
    if first.get("author"):
        parts.append("from " + " ".join(str(first["author"]).split())[:60])
    return " · ".join(p for p in parts if p) or None


def _parse_time(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def photo_sessions(assets):
    """[(asset, session key)] in time order: consecutive assets less than 2 h
    and 3 km apart share a session (distance only when both have GPS); the
    key is 'session:' + the first asset's Immich id."""
    ordered = sorted(
        (a for a in assets if _parse_time(a.get("taken_at"))),
        key=lambda a: (_parse_time(a["taken_at"]), str(a.get("immich_id"))),
    )
    out = []
    key = previous = None
    for a in ordered:
        at = _parse_time(a["taken_at"])
        same = previous is not None and at - _parse_time(previous["taken_at"]) < SESSION_GAP
        if same and None not in (a.get("lat"), a.get("lon"), previous.get("lat"), previous.get("lon")):
            same = km((a["lat"], a["lon"]), (previous["lat"], previous["lon"])) < SESSION_KM
        if not same:
            key = "session:" + str(a.get("immich_id") or a.get("id"))
        out.append((a, key))
        previous = a
    return out


def face_rows(faces, width=None, height=None):
    """Immich /faces answers -> store.replace_faces dicts (bbox in image pixels)."""
    out = []
    for f in faces or []:
        iw, ih = f.get("imageWidth") or width, f.get("imageHeight") or height
        sx = (width / iw) if width and iw else 1.0
        sy = (height / ih) if height and ih else 1.0
        x1, y1 = (f.get("boundingBoxX1") or 0) * sx, (f.get("boundingBoxY1") or 0) * sy
        x2, y2 = (f.get("boundingBoxX2") or 0) * sx, (f.get("boundingBoxY2") or 0) * sy
        person = f.get("person") or {}
        out.append(
            {
                "immich_face_id": f.get("id"),
                "immich_person_id": person.get("id"),
                "name": person.get("name") or None,
                "bbox": {
                    "x1": round(x1), "y1": round(y1), "x2": round(x2), "y2": round(y2),
                    "w": round(x2 - x1), "h": round(y2 - y1),
                },
                "score": f.get("score"),
            }
        )
    return out


def ocr_text_of(boxes):
    """Reading-order text of Immich OCR boxes (normalized coordinates): lines top to bottom."""
    items = []
    for b in boxes or []:
        text = (b.get("text") or "").strip()
        if not text:
            continue
        ys = [b.get(k) or 0 for k in ("y1", "y2", "y3", "y4")]
        xs = [b.get(k) or 0 for k in ("x1", "x2", "x3", "x4")]
        items.append((sum(ys) / 4, min(xs), max(ys) - min(ys), text))
    items.sort()
    lines, current, last_y, last_h = [], [], None, None
    for y, x, h, text in items:
        if last_y is not None and abs(y - last_y) > max(h, last_h or 0) * 0.6:
            lines.append(" ".join(t for _, t in sorted(current)))
            current = []
        current.append((x, text))
        last_y, last_h = y, h
    if current:
        lines.append(" ".join(t for _, t in sorted(current)))
    return "\n".join(lines)


class SqlQueries:
    """The SQL the pipeline needs beyond store.py (read only)."""

    def __init__(self, conn):
        self.conn = conn

    def _rows(self, sql, params=()):
        from psycopg.rows import dict_row

        with self.conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(sql, params).fetchall()

    def tables_ready(self):
        return bool(self._rows("SELECT to_regclass('tl.assets') IS NOT NULL AS ok")[0]["ok"])

    def linked_keys(self, channel):
        """{(external_id, ord): (asset_id, status)} of links in a channel."""
        rows = self._rows(
            "SELECT ma.external_id, ma.ord, a.id, a.status FROM tl.message_assets ma"
            " JOIN tl.channels c ON c.id=ma.channel_id JOIN tl.assets a ON a.id=ma.asset_id WHERE c.name=%s",
            (channel,),
        )
        return {(r["external_id"], r["ord"]): (r["id"], r["status"]) for r in rows}

    def linked(self, asset_id):
        return self._rows(
            "SELECT c.name AS channel, ma.external_id, ma.role, m.happened_at, t.title AS thread,"
            " coalesce(p.display_name, m.author_handle) AS author"
            " FROM tl.message_assets ma JOIN tl.channels c ON c.id=ma.channel_id"
            " LEFT JOIN tl.messages m ON m.channel_id=ma.channel_id AND m.external_id=ma.external_id"
            "  AND m.superseded_by IS NULL"
            " LEFT JOIN tl.threads t ON t.id=m.thread_id LEFT JOIN tl.people p ON p.id=m.person_id"
            " WHERE ma.asset_id=%s ORDER BY m.happened_at NULLS LAST, c.name, ma.external_id",
            (asset_id,),
        )

    def known_immich_ids(self):
        return {str(r["immich_id"]) for r in self._rows("SELECT immich_id FROM tl.assets WHERE immich_id IS NOT NULL")}

    def needs_describe(self, limit):
        """Uploaded (not Immich-native) assets whose Immich description is older than DESCRIBE_VERSION."""
        return self._rows(
            "SELECT id, immich_id, origin FROM tl.assets WHERE immich_id IS NOT NULL AND origin<>'immich'"
            " AND coalesce((enrich->>'describe_v')::int, 0) < %s ORDER BY id LIMIT %s",
            (DESCRIBE_VERSION, max(0, int(limit))),
        )

    def native_assets(self):
        return self._rows(
            "SELECT a.* FROM tl.assets a WHERE a.origin='immich' AND a.immich_id IS NOT NULL"
            " AND a.status='enriched' ORDER BY a.taken_at, a.id"
        )

    def face_identifiers(self):
        """Immich face identifiers; preview_only: every face of the cluster is
        on a shared-post cover (no link that is not a preview)."""
        return self._rows(
            "SELECT i.value AS immich_person_id, i.status, p.id AS person_id, p.display_name,"
            " (EXISTS(SELECT 1 FROM tl.asset_faces f WHERE f.identifier_id=i.id)"
            "  AND NOT EXISTS(SELECT 1 FROM tl.asset_faces f JOIN tl.message_assets ma ON ma.asset_id=f.asset_id"
            "  AND ma.role<>'preview' WHERE f.identifier_id=i.id)) AS preview_only"
            " FROM tl.identifiers i LEFT JOIN tl.people p ON p.id=i.person_id AND p.merged_into IS NULL"
            " WHERE i.system='immich' AND i.kind='face'"
        )

    # shared posts -----------------------------------------------------------
    def share_links(self):
        """Links of chat (Beeper) messages with their message text, for
        reclassify_previews."""
        return self._rows(
            "SELECT ma.channel_id, c.name AS channel, ma.external_id, ma.asset_id, ma.role, ma.source_ref,"
            " a.immich_id, a.tags, m.content"
            " FROM tl.message_assets ma JOIN tl.channels c ON c.id=ma.channel_id"
            " JOIN tl.assets a ON a.id=ma.asset_id"
            " JOIN tl.messages m ON m.channel_id=ma.channel_id AND m.external_id=ma.external_id AND m.superseded_by IS NULL"
            " WHERE c.plugin='beeper' ORDER BY ma.asset_id, c.name, ma.external_id"
        )

    def mark_previews(self, links):
        """Set role preview and source_ref.shared_url on (channel_id,
        external_id, asset_id, url) links; tag their assets shared-post and
        refresh their depicts links. Returns (links changed, assets tagged)."""
        changed = 0
        with self.conn.cursor() as cur:
            for channel_id, external_id, asset_id, url in links:
                cur.execute(
                    "UPDATE tl.message_assets SET role='preview',source_ref=source_ref||jsonb_build_object('shared_url',%s::text)"
                    " WHERE channel_id=%s AND external_id=%s AND asset_id=%s"
                    " AND (role<>'preview' OR source_ref->>'shared_url' IS DISTINCT FROM %s)",
                    (url, channel_id, external_id, asset_id, url),
                )
                changed += cur.rowcount
            ids = sorted({link[2] for link in links})
            cur.execute(
                "UPDATE tl.assets SET tags=array_append(tags,%s) WHERE id=ANY(%s) AND NOT %s=ANY(tags)",
                (SHARED_POST_TAG, ids, SHARED_POST_TAG),
            )
            tagged = cur.rowcount
            for start in range(0, len(ids), 500):
                cur.execute("SELECT tl.refresh_asset_depicts(%s::bigint[])", (ids[start : start + 500],))
        return changed, tagged

    # nsfw tag --------------------------------------------------------------
    def retag_nsfw(self, threshold):
        """Bring the nsfw tag of scored assets in line with the threshold.
        Returns (immich ids tagged, immich ids untagged), None for an asset
        not in Immich."""
        added = self._rows(
            "UPDATE tl.assets SET tags=array_append(tags,'nsfw') WHERE nsfw IS NOT NULL AND nsfw>=%s"
            " AND NOT 'nsfw'=ANY(tags) RETURNING immich_id",
            (threshold,),
        )
        removed = self._rows(
            "UPDATE tl.assets SET tags=array_remove(tags,'nsfw') WHERE nsfw IS NOT NULL AND nsfw<%s"
            " AND 'nsfw'=ANY(tags) RETURNING immich_id",
            (threshold,),
        )
        return (
            [str(r["immich_id"]) if r["immich_id"] else None for r in added],
            [str(r["immich_id"]) if r["immich_id"] else None for r in removed],
        )

    # location --------------------------------------------------------------
    def track_max_id(self):
        return self._rows("SELECT coalesce(max(id),0) AS n FROM tl.location_track")[0]["n"]

    def locate_candidates(self, window_min, max_accuracy_m, key, limit, max_id):
        """Assets without EXIF GPS (or placed by the track) whose location may
        change: never located with these settings, a track point arrived in
        their window since (id above the marker's track_id_max), or the point
        that placed them was withdrawn. Each with its nearest usable point
        (track_*; null when none)."""
        return self._rows(
            "SELECT a.id, a.immich_id, a.origin, a.taken_at, a.lat, a.lon, a.loc_method, a.loc_accuracy_m, a.place,"
            " a.enrich->'locate' AS marker, t.id AS track_id, t.lat AS track_lat, t.lon AS track_lon,"
            " t.accuracy_m AS track_accuracy_m"
            " FROM tl.assets a"
            " LEFT JOIN LATERAL (SELECT p.id, p.lat, p.lon, p.accuracy_m FROM tl.location_track p"
            "  WHERE p.at BETWEEN a.taken_at - make_interval(mins=>%(w)s) AND a.taken_at + make_interval(mins=>%(w)s)"
            "  AND p.accuracy_m <= %(acc)s AND NOT EXISTS(SELECT 1 FROM tl.retired_track r WHERE r.track_id=p.id)"
            "  ORDER BY abs(extract(epoch FROM p.at-a.taken_at)), p.accuracy_m, p.id LIMIT 1) t ON true"
            " WHERE a.taken_at IS NOT NULL AND a.status<>'missing' AND a.enrich ? 'pull'"
            " AND (a.lat IS NULL OR a.loc_method='track') AND a.loc_method IS DISTINCT FROM 'exif'"
            " AND EXISTS(SELECT 1 FROM tl.message_assets x WHERE x.asset_id=a.id AND x.role<>'preview')"
            " AND (NOT a.enrich ? 'locate' OR a.enrich->'locate'->>'key' IS DISTINCT FROM %(key)s"
            "  OR EXISTS(SELECT 1 FROM tl.location_track p WHERE p.id > coalesce((a.enrich->'locate'->>'track_id_max')::bigint,0)"
            "   AND p.id <= %(max)s"
            "   AND p.at BETWEEN a.taken_at - make_interval(mins=>%(w)s) AND a.taken_at + make_interval(mins=>%(w)s))"
            "  OR (a.loc_method='track' AND EXISTS(SELECT 1 FROM tl.retired_track r"
            "   WHERE r.track_id=(a.enrich->'locate'->>'track_id')::bigint)))"
            " ORDER BY a.id LIMIT %(n)s",
            dict(w=int(window_min), acc=float(max_accuracy_m), key=key, n=max(0, int(limit)), max=int(max_id)),
        )

    def geocode_pending(self, limit):
        return self._rows(
            "SELECT id, immich_id, place, enrich->'locate' AS marker FROM tl.assets"
            " WHERE loc_method='track' AND immich_id IS NOT NULL AND enrich->'locate'->'immich'->>'state'='pushed'"
            " ORDER BY id LIMIT %s",
            (max(0, int(limit)),),
        )

    def people_by_name(self, name):
        return self._rows(
            "SELECT id, display_name FROM tl.people WHERE merged_into IS NULL AND display_name=%s", (name,)
        )

    def status(self):
        out = {}
        out["by_status"] = {
            f"{r['origin']}/{r['status']}": r["n"]
            for r in self._rows("SELECT origin, status, count(*) n FROM tl.assets GROUP BY 1,2 ORDER BY 1,2")
        }
        out["stages_done"] = {
            r["k"]: r["n"]
            for r in self._rows("SELECT k, count(*) n FROM tl.assets, jsonb_object_keys(enrich) k GROUP BY 1 ORDER BY 1")
        }
        out["kinds"] = {
            str(r["kind"]): r["n"] for r in self._rows("SELECT kind, count(*) n FROM tl.assets GROUP BY 1 ORDER BY 2 DESC")
        }
        out["links"] = {
            r["name"]: r["n"]
            for r in self._rows(
                "SELECT c.name, count(*) n FROM tl.message_assets ma JOIN tl.channels c ON c.id=ma.channel_id GROUP BY 1 ORDER BY 1"
            )
        }
        out["dangling_links"] = self._rows(
            "SELECT count(*) n FROM tl.message_assets ma WHERE NOT EXISTS (SELECT 1 FROM tl.messages m"
            " WHERE m.channel_id=ma.channel_id AND m.external_id=ma.external_id)"
        )[0]["n"]
        return out

    def tag_distribution(self):
        return self._rows(
            "SELECT t AS tag, count(*) n FROM tl.assets, unnest(tags) t WHERE status='enriched' GROUP BY 1 ORDER BY 2 DESC, 1"
        )


class Pipeline:
    def __init__(
        self,
        conn,
        settings,
        *,
        store=None,
        queries=None,
        immich=None,
        ml=None,
        immich_db=None,
        nudenet=None,
        gemini_embed=None,
        resolvers=None,
        vocab=None,
        text_bank=None,
        commit=True,
        now=None,
        image_settings=None,
    ):
        if store is None:
            from . import store as real_store

            store = real_store
        self.conn = conn
        self.settings = settings
        self.store = store
        self.q = queries or SqlQueries(conn)
        self._immich = immich
        self._ml = ml
        self._immich_db = immich_db
        self._nudenet = nudenet
        self.gemini_embed = gemini_embed
        self.resolvers = resolvers or {}
        self.vocab = vocab
        self._bank = text_bank
        self.do_commit = commit
        self.now = now or (lambda: datetime.now(UTC))
        # The owner's image settings (tl.image_settings); injected in tests,
        # else read from the database once per sync (DEFAULTS before 0065).
        self._prefs_given = image_settings is not None
        self._prefs = {**user_settings.DEFAULTS, "available": True, **image_settings} if image_settings is not None else None

    @property
    def prefs(self):
        if self._prefs is None:
            if self.conn is None:
                self._prefs = dict(user_settings.DEFAULTS, available=False)
            else:
                self._prefs = user_settings.read(self.conn)
                self.commit()
        return self._prefs

    def refresh_prefs(self):
        if not self._prefs_given:
            self._prefs = None
        return self.prefs

    @property
    def threshold(self):
        return float(self.prefs["nsfw_threshold"])

    # collaborators, built on first use ------------------------------------
    @property
    def immich(self):
        if self._immich is None:
            from .immich import Immich

            self._immich = Immich(self.settings.immich_url, self.settings.immich_api_key)
        return self._immich

    @property
    def immich_db(self):
        if self._immich_db is None and self.settings.immich_db_url:
            from .immich import ImmichDB

            self._immich_db = ImmichDB(self.settings.immich_db_url)
        return self._immich_db

    @property
    def ml(self):
        if self._ml is None:
            from .immich import ImmichML

            self._ml = ImmichML(self.settings.immich_ml_url, self.settings.clip_model)
        return self._ml

    @property
    def bank(self):
        if self._bank is None:
            self._bank = tagging.TextBank(self.ml, self.vocabulary, self.settings.state_dir / "text_vectors.json")
        return self._bank

    @property
    def vocabulary(self):
        if self.vocab is None:
            self.vocab = tagging.load_vocabulary()
        return self.vocab

    def resolver(self, source):
        if source not in self.resolvers:
            self.resolvers[source] = source_module(source).Resolver(self.settings)
        return self.resolvers[source]

    def commit(self):
        if self.do_commit and hasattr(self.conn, "commit"):
            self.conn.commit()

    def close(self):
        for r in self.resolvers.values():
            r.close()
        if self._nudenet is not None and hasattr(self._nudenet, "close"):
            self._nudenet.close()

    # spool ------------------------------------------------------------------
    def spool_path(self, sha, mime=None):
        ext = sniff.EXTENSION_OF.get(mime or "", "bin")
        return self.settings.spool_dir / f"{sha}.{ext}"

    def find_spool(self, sha):
        matches = sorted(self.settings.spool_dir.glob(f"{sha}.*")) if self.settings.spool_dir.exists() else []
        return matches[0] if matches else None

    # discover + fetch --------------------------------------------------------
    def discover(self, source, limit=None):
        return source_module(source).discover(self.conn, self.settings, limit=limit)

    def backfill(self, source, limit=None, dry_run=False, candidates=None, by_channel=None):
        counts = Counter()
        if by_channel is None:
            by_channel = dry_run
        linked = {}
        tables = False
        try:
            tables = self.q.tables_ready()
        except Exception:
            tables = False
        if not dry_run and not tables:
            raise RuntimeError("tl.assets is missing: run `throughline migrate` (0063_images.sql) first")
        taken = 0
        for c in candidates if candidates is not None else self.discover(source):
            counts["discovered"] += 1
            if by_channel:
                counts[f"channel:{c.channel}:{c.skip or 'fetchable'}"] += 1
            if c.skip:
                counts["skipped:" + c.skip] += 1
                if c.skip in ("needs_email_export", "needs_beeper_export"):
                    counts["fetchable_after_export"] += 1
                continue
            counts["fetchable"] += 1
            counts["route:" + str(c.route)] += 1
            if c.message_known is False:
                counts["message_not_ingested"] += 1
            if tables:
                if c.channel not in linked:
                    linked[c.channel] = self.q.linked_keys(c.channel)
                hit = linked[c.channel].get((c.external_id, c.ord))
                if hit and hit[1] != "missing":
                    counts["already_linked"] += 1
                    continue
            if dry_run:
                continue
            if limit and taken >= limit:
                counts["deferred_by_limit"] += 1
                continue
            taken += 1
            try:
                fetched = self.resolver(source).fetch(c)
            except Exception as error:  # A resolver bug costs one item, not the run.
                LOG.warning("fetch failed %s %s: %s", c.channel, c.external_id, type(error).__name__)
                counts["fetch:error"] += 1
                continue
            counts["fetch:" + fetched.status] += 1
            if fetched.status != "ok" or not fetched.data:
                continue
            try:
                if self.ingest_bytes(c, fetched.data, fetched.mime) is None:
                    counts["skipped:not_image_bytes"] += 1
                    continue
                counts["stored"] += 1
            except Exception as error:
                if hasattr(self.conn, "rollback"):
                    self.conn.rollback()
                LOG.warning("store failed %s %s: %s", c.channel, c.external_id, error)
                counts["store:error"] += 1
        return counts

    def ingest_bytes(self, c, data, mime=None):
        """Store, spool and link the bytes of one candidate; the asset id, or
        None (nothing stored) when the bytes do not sniff as an image (an
        HTML error page, a truncated download, a video)."""
        sha = hashlib.sha256(data).hexdigest()
        sniffed, width, height = sniff.sniff(data)
        if not sniffed:
            LOG.info("not image bytes %s %s (%d bytes)", c.channel, c.external_id, len(data))
            return None
        mime = sniffed
        asset_id = self.store.upsert_asset(
            self.conn,
            sha256=sha,
            origin=c.origin,
            mime=mime,
            bytes=len(data),
            width=width or c.width,
            height=height or c.height,
            original_name=c.name,
        )
        row = self.store.get_asset(self.conn, asset_id)
        if row.get("status") == "missing" and row.get("immich_id"):
            # Gone from Immich (marked by an older pull): upload it again.
            self.store.set_asset(self.conn, asset_id, immich_id=None, status="pending", drop_enrich=["pull"])
            row = self.store.get_asset(self.conn, asset_id)
        if not row.get("immich_id"):
            path = self.spool_path(sha, mime)
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".part")
                tmp.write_bytes(data)
                tmp.replace(path)
            facts = sniff.exif_facts(data)
            local = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in facts.items() if v}
            cols = {"status": "pending"} if row.get("status") == "missing" else {}
            self.store.set_asset(self.conn, asset_id, merge_enrich={"local": local} if local else None, **cols)
        self.store.link_message(
            self.conn,
            channel=c.channel,
            external_id=c.external_id,
            asset_id=asset_id,
            ord=c.ord,
            role=c.role,
            source_ref=c.source_ref,
        )
        if c.role == "preview":
            tags = list(self.store.get_asset(self.conn, asset_id).get("tags") or [])
            if SHARED_POST_TAG not in tags:
                self.store.set_asset(self.conn, asset_id, tags=tags + [SHARED_POST_TAG])
        self.commit()
        return asset_id

    # upload ------------------------------------------------------------------
    def upload(self, limit=100):
        counts = Counter()
        for row in self.store.assets_needing(self.conn, "upload", limit):
            path = self.find_spool(row["sha256"])
            if path is None:
                self.store.set_asset(self.conn, row["id"], status="missing", merge_enrich={"upload": {"error": "spool_missing"}})
                self.commit()
                counts["spool_missing"] += 1
                continue
            data = path.read_bytes()
            links = [dict(link, label=label_of(link["channel"])) for link in self.q.linked(row["id"])]
            local = (row.get("enrich") or {}).get("local") or {}
            created = None
            if local.get("taken"):
                created = datetime.fromisoformat(local["taken"])
                if created.tzinfo is None:
                    created = created.astimezone()
            if created is None:
                times = [_parse_time(link.get("happened_at")) for link in links if link.get("happened_at")]
                created = min(times) if times else self.now()
            ext = sniff.EXTENSION_OF.get(row.get("mime") or "", path.suffix.lstrip("."))
            name = row.get("original_name") or f"{row['sha256'][:16]}.{ext}"
            if "." not in name:
                name = f"{name}.{ext}"
            try:
                immich_id, how = self.immich.upload(
                    data,
                    name,
                    created_at=created,
                    metadata={"sha256": row["sha256"], "origin": row["origin"]},
                )
            except Exception as error:
                LOG.warning("upload failed asset %s: %s", row["id"], str(error)[:200])
                counts["upload:error"] += 1
                continue
            self.store.set_asset(
                self.conn,
                row["id"],
                immich_id=immich_id,
                status="uploaded",
                merge_enrich={"upload": {"v": 1, "duplicate": how == "duplicate", "at": self.now().isoformat()}},
            )
            self.commit()
            counts["upload:" + how] += 1
            try:
                description = describe(links)
                if description:
                    self.immich.update(immich_id, description=description)
                self.store.set_asset(self.conn, row["id"], merge_enrich={"describe_v": DESCRIBE_VERSION})
                self.commit()
                for label in sorted({link["label"] for link in links if link.get("label")}):
                    self.immich.add_to_album(f"Source · {label}", [immich_id])
                values = [f"source/{row['origin']}"]
                if SHARED_POST_TAG in (row.get("tags") or []) or any(link.get("role") == "preview" for link in links):
                    self.immich.add_to_album(SHARED_ALBUM, [immich_id])
                    values.append(SHARED_POST_TAG)
                self.immich.tag([immich_id], values)
            except Exception as error:
                counts["organize:error"] += 1
                LOG.warning("organize failed asset %s: %s", row["id"], str(error)[:200])
            try:
                path.unlink()
            except OSError:
                pass
        return counts

    # pull --------------------------------------------------------------------
    def _ready(self, rows):
        ids = [str(r["immich_id"]) for r in rows]
        if self.immich_db is not None:
            status = self.immich_db.job_status(ids)
            return {
                i: bool(s and s["metadata"] and s["clip"] and s["faces"] and s["ocr"]) for i, s in status.items()
            }
        idle = self.immich.ml_idle()
        return {i: idle for i in ids}

    def pull(self, limit=100):
        counts = Counter()
        rows = self.store.assets_needing(self.conn, "pull", limit)
        if not rows:
            return counts
        ready = self._ready(rows)
        vectors = self.immich_db.clip_vectors([str(r["immich_id"]) for r in rows]) if self.immich_db else {}
        for row in rows:
            iid = str(row["immich_id"])
            uploaded = _parse_time(((row.get("enrich") or {}).get("upload") or {}).get("at"))
            patient = uploaded is None or self.now() - uploaded > PULL_PATIENCE
            if not ready.get(iid) and not patient:
                counts["waiting_for_immich"] += 1
                continue
            try:
                self._pull_one(row, vectors.get(iid))
                counts["pulled" if ready.get(iid) else "pulled_partial"] += 1
            except Exception as error:
                if hasattr(self.conn, "rollback"):
                    self.conn.rollback()
                status = getattr(error, "status", None)
                if status == 404:
                    # Gone from Immich: forget the id so the asset uploads
                    # again (from the spool, or after the next backfill
                    # fetches its bytes once upload marks it missing).
                    self.store.set_asset(
                        self.conn,
                        row["id"],
                        immich_id=None,
                        status="pending",
                        drop_enrich=["pull"],
                        merge_enrich={"gone_from_immich": {"immich_id": iid, "at": self.now().isoformat()}},
                    )
                    self.commit()
                    counts["missing_in_immich"] += 1
                else:
                    LOG.warning("pull failed asset %s: %s", row["id"], str(error)[:200])
                    counts["pull:error"] += 1
        return counts

    def _pull_one(self, row, vector):
        iid = str(row["immich_id"])
        info = self.immich.asset(iid)
        exif = info.get("exifInfo") or {}
        width = exif.get("exifImageWidth") or info.get("width") or row.get("width")
        height = exif.get("exifImageHeight") or info.get("height") or row.get("height")
        orientation = str(exif.get("orientation") or "")
        if orientation in ("5", "6", "7", "8") and width and height:
            width, height = height, width
        cols = {"width": width, "height": height}
        taken = _parse_time(exif.get("dateTimeOriginal"))
        if taken:
            cols["taken_at"] = taken
        if exif.get("latitude") is not None and exif.get("longitude") is not None:
            lat, lon = float(exif["latitude"]), float(exif["longitude"])
            pushed = row.get("loc_method") == "track" and None not in (row.get("lat"), row.get("lon")) and (
                abs(lat - float(row["lat"])) < 1e-4 and abs(lon - float(row["lon"])) < 1e-4
            )
            if not pushed:  # Immich echoes a location we pushed: still the track's.
                cols["lat"], cols["lon"] = lat, lon
                cols["loc_method"], cols["loc_accuracy_m"] = "exif", None
        place = {k: exif.get(k) for k in ("city", "state", "country") if exif.get(k)}
        if place:
            cols["place"] = place
        if info.get("type") == "VIDEO":
            cols["kind"] = "video"
            duration = info.get("duration")
            if isinstance(duration, str) and ":" in duration:
                h, m, s = duration.split(":")
                cols["duration_s"] = int(h) * 3600 + int(m) * 60 + float(s)
        if not row.get("mime") and info.get("originalMimeType"):
            cols["mime"] = info["originalMimeType"]
        boxes = self.immich.ocr(iid)
        cols["ocr_text"] = ocr_text_of(boxes)
        faces = face_rows(self.immich.faces(iid), width, height)
        if vector is None and self.immich_db is not None:
            vector = self.immich_db.clip_vectors([iid]).get(iid)
        compact = [
            {k: round(b.get(k) or 0, 4) for k in ("y1", "y2", "y3", "y4")} | {"text": (b.get("text") or "")[:40]}
            for b in boxes[:60]
        ]
        enrich = {
            "pull": {
                "v": PULL_VERSION,
                "exif": {k: exif.get(k) for k in ("make", "model", "lensModel") if exif.get(k)},
                "faces": len(faces),
                "ocr_boxes": compact,
                "siglip": vector is not None,
            }
        }
        self.store.set_asset(self.conn, row["id"], merge_enrich=enrich, **cols)
        self.store.replace_faces(self.conn, row["id"], faces)
        if vector is not None:
            self.store.put_vector(self.conn, row["id"], SIGLIP, vector)
        self.commit()

    # locate ------------------------------------------------------------------
    def locate(self, limit=1000):
        """Assets without EXIF GPS take the nearest location track point
        (tl.location_track, not withdrawn) within ±location_window_min of
        taken_at and at most location_max_accuracy_m: lat/lon, loc_method
        'track', loc_accuracy_m. EXIF always wins (pull). With
        location_fallback 'throughline_and_immich' the location is also
        written to Immich (latitude/longitude, description + " · location
        from history"); Immich reverse-geocodes it and a later run reads the
        place back. enrich.locate = {v, key (settings), track_id_max (the
        newest track point seen), track_id, at, immich}: an asset is looked at
        again only when a newer point falls in its window, its point was
        withdrawn, or the settings changed. Needs 0065; 'off' does nothing."""
        counts = Counter()
        prefs = self.prefs
        mode = prefs.get("location_fallback")
        if not prefs.get("available") or mode == "off":
            return counts
        immich = mode == "throughline_and_immich"
        if immich:
            counts.update(self.geocode_readback())
        window, accuracy = int(prefs["location_window_min"]), int(prefs["location_max_accuracy_m"])
        key = f"{window}:{accuracy}"
        max_id = self.q.track_max_id()
        for row in self.q.locate_candidates(window, accuracy, key, limit, max_id):
            marker = dict(row.get("marker") or {})
            marker.update(v=LOCATE_VERSION, key=key, track_id_max=int(max_id), at=self.now().isoformat())
            cols = {}
            if row.get("track_id") is not None:
                lat, lon = float(row["track_lat"]), float(row["track_lon"])
                acc = row.get("track_accuracy_m")
                acc = float(acc) if acc is not None else None
                same = row.get("loc_method") == "track" and (row.get("lat"), row.get("lon"), row.get("loc_accuracy_m")) == (lat, lon, acc)
                marker["track_id"] = int(row["track_id"])
                if not same:
                    cols = dict(lat=lat, lon=lon, loc_method="track", loc_accuracy_m=acc)
                    counts["moved" if row.get("loc_method") == "track" else "located"] += 1
                    if immich and row.get("immich_id"):
                        marker["immich"] = self._push_location(row, lat, lon, counts)
                else:
                    counts["unchanged"] += 1
            elif row.get("loc_method") == "track":
                # Its point was withdrawn (or the settings exclude it now).
                cols = dict(lat=None, lon=None, loc_method=None, loc_accuracy_m=None, place=None)
                marker.pop("track_id", None)
                marker.pop("immich", None)
                counts["cleared"] += 1
            else:
                counts["no_track_point"] += 1
            self.store.set_asset(self.conn, row["id"], merge_enrich={"locate": marker}, **cols)
            self.commit()
        return +counts

    def _push_location(self, row, lat, lon, counts):
        """Write the location to the Immich asset with the description suffix;
        the marker's immich part."""
        iid = str(row["immich_id"])
        try:
            info = self.immich.asset(iid)
            description = ((info.get("exifInfo") or {}).get("description") or "").strip()
            if not description and row.get("origin") != "immich":
                links = [dict(link, label=label_of(link["channel"])) for link in self.q.linked(row["id"])]
                description = describe(links) or ""
            if LOCATED_SUFFIX.strip() not in description:
                description = (description + LOCATED_SUFFIX) if description else LOCATED_SUFFIX.lstrip(" ·")
            self.immich.update(iid, latitude=lat, longitude=lon, description=description)
        except Exception as error:
            counts["immich:error"] += 1
            LOG.warning("location push failed asset %s: %s", row["id"], str(error)[:200])
            return {"state": "error", "error": str(error)[:200], "at": self.now().isoformat()}
        counts["pushed_to_immich"] += 1
        return {"state": "pushed", "at": self.now().isoformat()}

    def geocode_readback(self, limit=500):
        """Read Immich's reverse geocoding (city, state, country) of locations
        pushed earlier into tl.assets.place."""
        counts = Counter()
        pending = getattr(self.q, "geocode_pending", None)
        if pending is None:
            return counts
        for row in pending(limit):
            marker = dict(row.get("marker") or {})
            state = dict(marker.get("immich") or {})
            pushed = _parse_time(state.get("at"))
            if pushed and self.now() - pushed < GEOCODE_DELAY:
                counts["geocode_waiting"] += 1
                continue
            try:
                exif = self.immich.asset(str(row["immich_id"])).get("exifInfo") or {}
            except Exception as error:
                counts["geocode:error"] += 1
                LOG.warning("geocode readback failed asset %s: %s", row["id"], str(error)[:200])
                continue
            place = {k: exif.get(k) for k in ("city", "state", "country") if exif.get(k)}
            cols = {}
            if place:
                cols["place"] = place
                state.update(state="geocoded", geocoded_at=self.now().isoformat())
                counts["geocoded"] += 1
            elif pushed is None or self.now() - pushed > GEOCODE_PATIENCE:
                state.update(state="no_place")
                counts["geocode_no_place"] += 1
            else:
                counts["geocode_waiting"] += 1
                continue
            marker["immich"] = state
            self.store.set_asset(self.conn, row["id"], merge_enrich={"locate": marker}, **cols)
            self.commit()
        return counts

    # shared posts ------------------------------------------------------------
    def reclassify_previews(self, dry_run=False, batch=500):
        """Existing links of chat messages whose only text is a link to a post
        or reel (images/shared.py) become previews: role 'preview',
        source_ref.shared_url, tag 'shared-post' on the asset (and in Immich,
        plus album 'Shared posts'); their depicts links go (faces on a cover
        name nobody). Idempotent. dry_run counts only (read only) and also
        counts look-alike links that are not marked (stories, threads,
        facebook) per network."""
        report = {"by_channel": {}, "other_shares": {}}
        found, assets, already = [], {}, 0
        for link in self.q.share_links():
            channel = link["channel"]
            per = report["by_channel"].setdefault(channel, Counter())
            per["links"] += 1
            share = shared.shared_post(link.get("content"))
            if not share:
                other = shared.other_share(link.get("content"))
                if other:
                    report["other_shares"].setdefault(channel, Counter())[other] += 1
                continue
            per["preview_links"] += 1
            per["network:" + share[0]] += 1
            ref = link.get("source_ref") or {}
            if link.get("role") == "preview" and ref.get("shared_url") == share[1]:
                already += 1
            found.append((link["channel_id"], link["external_id"], link["asset_id"], share[1]))
            assets.setdefault(link["asset_id"], (link.get("immich_id"), channel))
        for channel in report["by_channel"]:
            ids = {a for a, (_, ch) in assets.items() if ch == channel}
            report["by_channel"][channel]["preview_assets"] = len(ids)
        report["by_channel"] = {k: dict(v) for k, v in sorted(report["by_channel"].items())}
        report["other_shares"] = {k: dict(v) for k, v in sorted(report["other_shares"].items())}
        report["preview_links"] = len(found)
        report["preview_assets"] = len(assets)
        report["already_marked"] = already
        if dry_run:
            return report
        if not self.prefs.get("available"):
            raise RuntimeError("role 'preview' needs migration 0065: run `throughline migrate` first")
        changed = tagged = 0
        for start in range(0, len(found), batch):
            c, t = self.q.mark_previews(found[start : start + batch])
            changed, tagged = changed + c, tagged + t
            self.commit()
        report["links_changed"], report["assets_tagged"] = changed, tagged
        immich_ids = sorted({str(i) for i, _ in assets.values() if i})
        done = 0
        try:
            for start in range(0, len(immich_ids), batch):
                part = immich_ids[start : start + batch]
                self.immich.tag(part, [SHARED_POST_TAG])
                self.immich.add_to_album(SHARED_ALBUM, part)
                done += len(part)
        except Exception as error:
            report["immich_error"] = f"{type(error).__name__}: {str(error)[:200]}"
        report["immich_organized"] = done
        if "immich_error" not in report:
            self._previews_marker().parent.mkdir(parents=True, exist_ok=True)
            self._previews_marker().write_text(
                json.dumps({"at": self.now().isoformat(), "preview_links": len(found), "links_changed": changed}),
                encoding="utf-8",
            )
        return report

    def _previews_marker(self):
        return self.settings.state_dir / "reclassify_previews.json"

    def previews_reclassified(self):
        """Whether reclassify_previews has completed once (state_dir marker)."""
        return self._previews_marker().exists()

    # status ------------------------------------------------------------------
    def _finish(self, asset_id):
        row = self.store.get_asset(self.conn, asset_id)
        enrich = row.get("enrich") or {}
        if row.get("status") == "uploaded" and all(k in enrich for k in ("pull", "nsfw", "tags")):
            self.store.set_asset(self.conn, asset_id, status="enriched")

    # nsfw --------------------------------------------------------------------
    @property
    def nudenet(self):
        if self._nudenet is None:
            self._nudenet = nsfw_mod.NudeNetBridge(self.settings.nudenet_python)
        return self._nudenet

    def nsfw(self, limit=100, batch=16):
        counts = Counter()
        rows = self.store.assets_needing(self.conn, "nsfw", limit)
        if not rows:
            return counts
        with tempfile.TemporaryDirectory(prefix="tl-images-") as tmp:
            for start in range(0, len(rows), batch):
                chunk = rows[start : start + batch]
                paths = {}
                for row in chunk:
                    if row.get("kind") == "video" or (row.get("mime") or "").startswith("video/"):
                        self.store.set_asset(self.conn, row["id"], merge_enrich={"nsfw": {"v": nsfw_mod.VERSION, "skipped": "video"}})
                        counts["skipped_video"] += 1
                        continue
                    try:
                        data = self.immich.thumbnail(str(row["immich_id"]), "preview")
                    except Exception as error:
                        counts["preview:error"] += 1
                        LOG.warning("preview failed asset %s: %s", row["id"], str(error)[:120])
                        continue
                    path = Path(tmp) / f"{row['id']}.jpg"
                    path.write_bytes(data)
                    paths[str(path)] = row
                if not paths:
                    self.commit()
                    continue
                try:
                    results = self.nudenet.classify(list(paths))
                except nsfw_mod.BridgeError as error:
                    counts["nudenet_unavailable"] += len(paths)
                    LOG.warning("nudenet unavailable: %s", error)
                    break
                for path, row in paths.items():
                    result = results.get(path) or {"error": "no result"}
                    if "error" in result:
                        # Not done: retried next run; given up (done, nsfw
                        # left null, still unsafe) after NSFW_ATTEMPTS.
                        enrich = row.get("enrich") or {}
                        attempts = int((enrich.get("nsfw_error") or {}).get("attempts") or 0) + 1
                        error = str(result["error"])[:200]
                        stamp = {"nsfw_error": {"error": error, "attempts": attempts, "at": self.now().isoformat()}}
                        if attempts >= NSFW_ATTEMPTS:
                            stamp["nsfw"] = {"v": nsfw_mod.VERSION, "error": error, "gave_up": True, "attempts": attempts}
                            counts["nsfw:gave_up"] += 1
                        else:
                            counts["nsfw:error"] += 1
                        self.store.set_asset(self.conn, row["id"], merge_enrich=stamp)
                        Path(path).unlink(missing_ok=True)
                        if attempts < NSFW_ATTEMPTS:
                            continue
                    else:
                        self.store.set_asset(
                            self.conn,
                            row["id"],
                            nsfw=result["nsfw"],
                            nsfw_labels=result["labels"],
                            merge_enrich={"nsfw": {"v": nsfw_mod.VERSION}},
                            drop_enrich=["nsfw_error"],
                        )
                        counts["nsfw_flagged" if result["nsfw"] >= self.threshold else "nsfw_clean"] += 1
                    self._finish(row["id"])
                    Path(path).unlink(missing_ok=True)
                self.commit()
        return counts

    # tags + kind ---------------------------------------------------------------
    def signals_for(self, row, links=None):
        enrich = row.get("enrich") or {}
        pull = enrich.get("pull") or {}
        local = enrich.get("local") or {}
        exif = dict(local)
        exif.update(pull.get("exif") or {})
        hints = [link.get("channel") for link in links or []] + [row.get("original_name")]
        return tagging.signals_of(
            mime=row.get("mime"),
            width=row.get("width"),
            height=row.get("height"),
            name=row.get("original_name"),
            exif=exif,
            ocr_text=row.get("ocr_text"),
            ocr_boxes=pull.get("ocr_boxes"),
            faces=self._faces_of(row) if pull.get("faces") else [],
            nsfw=row.get("nsfw"),
            origin=row.get("origin"),
            source_hints=hints,
            nsfw_threshold=self.threshold,
        )

    def _faces_of(self, row):
        getter = getattr(self.q, "faces", None)
        if getter:
            return getter(row["id"])
        try:
            from psycopg.rows import dict_row

            with self.conn.cursor(row_factory=dict_row) as cur:
                return cur.execute("SELECT bbox FROM tl.asset_faces WHERE asset_id=%s", (row["id"],)).fetchall()
        except Exception:
            return []

    def retag_nsfw(self):
        """The nsfw tag follows the current threshold (scored assets only), in
        Throughline and Immich. Runs with the tags stage; needs 0065."""
        counts = Counter()
        retag = getattr(self.q, "retag_nsfw", None)
        if retag is None or not self.prefs.get("available"):
            return counts
        added, removed = retag(self.threshold)
        self.commit()
        counts["nsfw_tag_added"], counts["nsfw_tag_removed"] = len(added), len(removed)
        added, removed = [i for i in added if i], [i for i in removed if i]
        try:
            if added:
                self.immich.tag(added, ["nsfw"])
            if removed:
                self.immich.untag(removed, ["nsfw"])
        except Exception as error:
            counts["organize:error"] += 1
            LOG.warning("nsfw retag in immich failed: %s", str(error)[:200])
        return +counts

    def tags(self, limit=100):
        counts = self.retag_nsfw()
        rows = self.store.assets_needing(self.conn, "tags", limit)
        if not rows:
            return counts
        vectors = self.immich_db.clip_vectors([str(r["immich_id"]) for r in rows]) if self.immich_db else {}
        vocab = self.vocabulary
        version = tagging.vocabulary_hash(vocab)
        for row in rows:
            iid = str(row["immich_id"])
            vector = vectors.get(iid)
            is_video = row.get("kind") == "video" or (row.get("mime") or "").startswith("video/")
            if vector is None and not is_video:
                counts["no_siglip_vector"] += 1
                continue
            signals = self.signals_for(row, self.q.linked(row["id"]))
            if is_video:
                result = {"kind": "video", "tags": [], "kind_scores": {}, "tag_scores": {}}
            else:
                result = tagging.classify(self.bank.similarities(vector), signals, vocab)
            if row.get("origin") == "capture":
                # A desktop screenshot is a screenshot whatever it shows.
                result["kind"] = "screenshot"
            sticky = [t for t in STICKY_TAGS if t in (row.get("tags") or []) and t not in result["tags"]]
            if sticky:
                result["tags"] = list(result["tags"]) + sticky
            self.store.set_asset(
                self.conn,
                row["id"],
                kind=result["kind"],
                tags=result["tags"],
                merge_enrich={
                    "tags": {
                        "v": version,
                        "kind_scores": result["kind_scores"],
                        "tag_scores": result["tag_scores"],
                    }
                },
            )
            self._finish(row["id"])
            self.commit()
            counts["kind:" + result["kind"]] += 1
            if "meme" in result["tags"]:
                counts["meme"] += 1
            try:
                # A re-run replaces the old classification in Immich too.
                old_tags = set(row.get("tags") or []) - {"nsfw", *STICKY_TAGS}
                old_kind = row.get("kind")
                stale = sorted(old_tags - set(result["tags"]))
                if old_kind and old_kind != result["kind"]:
                    stale.append(f"kind/{old_kind}")
                    if KIND_ALBUMS.get(old_kind):
                        self.immich.remove_from_album(KIND_ALBUMS[old_kind], [iid])
                if stale:
                    self.immich.untag([iid], stale)
                if "meme" in old_tags and "meme" not in result["tags"]:
                    self.immich.remove_from_album(MEME_ALBUM, [iid])
                self.immich.tag([iid], [f"kind/{result['kind']}"] + list(result["tags"]))
                album = KIND_ALBUMS.get(result["kind"])
                if album:
                    self.immich.add_to_album(album, [iid])
                if "meme" in result["tags"]:
                    self.immich.add_to_album(MEME_ALBUM, [iid])
            except Exception as error:
                counts["organize:error"] += 1
                LOG.warning("tagging in immich failed asset %s: %s", row["id"], str(error)[:200])
        return counts

    # gemini --------------------------------------------------------------------
    def gemini(self, limit=100, batch=16):
        counts = Counter()
        rows = self.store.assets_needing(self.conn, "gemini", limit)
        rows = [r for r in rows if r.get("kind") != "video"]
        for start in range(0, len(rows), batch):
            chunk = rows[start : start + batch]
            images = []
            for row in chunk:
                try:
                    images.append((row, self.immich.thumbnail(str(row["immich_id"]), "preview")))
                except Exception:
                    counts["preview:error"] += 1
            try:
                vectors = gemini_door.embed([(d, "image/jpeg") for _, d in images], self.gemini_embed)
            except gemini_door.GeminiPending as error:
                pending = len(rows) - start
                stamp = {"gemini_pending": {"reason": str(error)[:200], "at": self.now().isoformat()}}
                for row in rows[start:]:
                    self.store.set_asset(self.conn, row["id"], merge_enrich=stamp)
                self.commit()
                counts["pending"] += pending
                LOG.warning("gemini pending (%s); %d assets left for a later run", str(error)[:80], pending)
                return counts
            except gemini_door.GeminiFailed:
                vectors = []
                for row, data in images:
                    try:
                        vectors.append(gemini_door.embed([(data, "image/jpeg")], self.gemini_embed)[0])
                    except gemini_door.GeminiPending:
                        counts["pending"] += 1
                        vectors.append("pending")
                    except gemini_door.GeminiFailed as error:
                        vectors.append(error)
            for (row, _), vector in zip(images, vectors):
                if isinstance(vector, str):
                    continue
                if isinstance(vector, Exception):
                    self.store.set_asset(self.conn, row["id"], merge_enrich={"gemini": {"error": str(vector)[:200]}})
                    counts["gemini:error"] += 1
                    continue
                self.store.put_vector(self.conn, row["id"], GEMINI, vector)
                self.store.set_asset(self.conn, row["id"], merge_enrich={"gemini": {"v": 1}, "gemini_pending": None})
                counts["embedded"] += 1
            self.commit()
        return counts

    # Immich-native -------------------------------------------------------------
    def native(self, limit=100):
        """Assets in Immich that Throughline did not upload: store and enrich them too."""
        counts = Counter()
        known = self.q.known_immich_ids()
        for item in self.immich.search():
            iid = str(item["id"])
            if iid in known:
                continue
            counts["discovered"] += 1
            if item.get("isTrashed"):
                counts["skipped:trashed"] += 1
                continue
            if str(item.get("originalFileName") or "").startswith(CAPTURE_PREFIX):
                # Desktop screenshots: the capture source adopts them with
                # their record; they are not camera-roll photos.
                counts["skipped:capture"] += 1
                continue
            if limit and counts["stored"] >= limit:
                counts["deferred_by_limit"] += 1
                continue
            try:
                data = self.immich.original(iid)
            except Exception:
                counts["download:error"] += 1
                continue
            sha = hashlib.sha256(data).hexdigest()
            asset_id = self.store.upsert_asset(
                self.conn,
                sha256=sha,
                origin="immich",
                mime=item.get("originalMimeType"),
                bytes=len(data),
                width=item.get("width"),
                height=item.get("height"),
                original_name=item.get("originalFileName"),
            )
            row = self.store.get_asset(self.conn, asset_id)
            if row.get("immich_id") and str(row["immich_id"]) != iid:
                counts["duplicate_bytes_in_immich"] += 1
                self.commit()
                continue
            if not row.get("immich_id"):
                self.store.set_asset(
                    self.conn,
                    asset_id,
                    immich_id=iid,
                    status="uploaded",
                    merge_enrich={"upload": {"v": 1, "native": True, "at": self.now().isoformat()}},
                )
            known.add(iid)
            self.commit()
            counts["stored"] += 1
        return counts

    def register_native_channel(self):
        from ..ingest import register_channel

        return register_channel(
            self.conn,
            {
                "name": NATIVE_CHANNEL,
                "plugin": PLUGIN,
                "description": "Photos and videos uploaded to Immich directly (phone app, web); one message per asset, threaded by photo session (< 2 h and < 3 km apart).",
                "fields": [
                    {"name": "asset_id", "type": "integer", "description": "tl.assets id"},
                    {"name": "immich_id", "type": "text", "description": "Immich asset id"},
                    {"name": "kind", "type": "text", "description": "photo, screenshot, document, artwork, video or other"},
                    {"name": "tags", "type": "json", "description": "Zero-shot tags"},
                    {"name": "place", "type": "json", "description": "{city,state,country} from Immich reverse geocoding"},
                    {"name": "original_name", "type": "text", "description": "File name in Immich"},
                ],
            },
        )

    def native_message(self, row, session):
        """The photos.immich message of a native asset. Its text never names
        people (faces are for unrestricted askers; a message is gated by
        channel only), and an unsafe asset (store.asset_safe) is "[photo]"
        with place and time only: no tags, caption or OCR."""
        owner = self.settings.owner_identifier.split(":", 2)
        taken = _parse_time(row["taken_at"])
        safe = asset_safe(row, self.threshold, self.prefs["unscanned"])
        if safe:
            content = self.store.render_text(row, ())
        else:
            header = ["photo"]
            place = _place_text(row.get("place"))
            if place:
                header.append(place)
            header.append(taken.isoformat(timespec="minutes"))
            content = "[" + " · ".join(header) + "]"
        message = {
            "channel": NATIVE_CHANNEL,
            "external_id": "immich:" + str(row["immich_id"]),
            "thread": {"external_id": session, "title": f"Photos {taken.date().isoformat()}"},
            "happened_at": taken.isoformat(),
            "author": {"system": owner[0], "kind": owner[1], "value": owner[2], "display_name": self.settings.owner_name},
            "content": content,
            "fields": {
                "asset_id": int(row["id"]),
                "immich_id": str(row["immich_id"]),
                "kind": row.get("kind") if safe else "photo",
                "tags": list(row.get("tags") or []) if safe else [],
                "place": row.get("place") if isinstance(row.get("place"), (dict, type(None))) else json.loads(row["place"]),
                "original_name": row.get("original_name"),
            },
            "payload": {"immich_id": str(row["immich_id"]), "sha256": row["sha256"]},
        }
        if row.get("lat") is not None and row.get("lon") is not None:
            message["location"] = {"lat": float(row["lat"]), "lon": float(row["lon"])}
        return message

    def native_messages(self, batch=500):
        """(Re)deliver one photos.immich message per enriched native asset and link it (role self)."""
        from ..ingest import ingest
        from ..models import Ingest

        counts = Counter()
        assets = self.q.native_assets()
        if not assets:
            return counts
        self.register_native_channel()
        messages = [(row, self.native_message(row, session)) for row, session in photo_sessions(assets)]
        for start in range(0, len(messages), batch):
            part = messages[start : start + batch]
            result = ingest(self.conn, Ingest(plugin=PLUGIN, messages=[m for _, m in part]))
            for key in ("inserted", "new_versions", "unchanged"):
                counts[key] += result.get(key, 0)
            counts["rejected"] += len(result.get("rejected") or [])
            for row, m in part:
                self.store.link_message(
                    self.conn, channel=NATIVE_CHANNEL, external_id=m["external_id"], asset_id=row["id"], role="self",
                    source_ref={"immich_id": str(row["immich_id"])},
                )
            self.commit()
        return counts

    # descriptions ----------------------------------------------------------
    def redescribe(self, limit=500):
        """Rewrite the Immich description of uploaded assets described in an
        older format (before DESCRIBE_VERSION 2 it held message text).
        Idempotent: progress is enrich.describe_v."""
        counts = Counter()
        for row in self.q.needs_describe(limit):
            links = [dict(link, label=label_of(link["channel"])) for link in self.q.linked(row["id"])]
            try:
                self.immich.update(str(row["immich_id"]), description=describe(links) or "")
            except Exception as error:
                status = getattr(error, "status", None)
                counts["missing_in_immich" if status == 404 else "update:error"] += 1
                LOG.warning("redescribe failed asset %s: %s", row["id"], str(error)[:200])
                continue
            self.store.set_asset(self.conn, row["id"], merge_enrich={"describe_v": DESCRIBE_VERSION})
            self.commit()
            counts["redescribed"] += 1
        return counts

    # people ----------------------------------------------------------------
    def people_sync(self, dry_run=False):
        """Names both ways: Throughline people linked (confirmed) to an Immich
        face cluster name that Immich person; an Immich person name that
        exactly matches a Throughline display name is printed as a suggestion."""
        report = {"renamed_in_immich": [], "suggestions": [], "counts": Counter()}
        linked = {}
        for r in self.q.face_identifiers():
            linked[str(r["immich_person_id"]).lower()] = r
        for person in self.immich.people():
            pid = str(person["id"]).lower()
            name = (person.get("name") or "").strip()
            link = linked.get(pid)
            report["counts"]["immich_people"] += 1
            if link and link.get("person_id") and link.get("status") == "confirmed" and link.get("display_name"):
                if name != link["display_name"]:
                    report["renamed_in_immich"].append({"immich_person_id": person["id"], "from": name, "to": link["display_name"]})
                    if not dry_run:
                        self.immich.rename_person(person["id"], link["display_name"])
                continue
            if link and link.get("preview_only"):
                # Faces seen only on shared-post covers: not people the owner met.
                report["counts"]["skipped_preview_only"] += 1
                continue
            if name and not (link and link.get("person_id") and link.get("status") == "confirmed"):
                for match in self.q.people_by_name(name):
                    report["suggestions"].append(
                        {
                            "immich_person_id": person["id"],
                            "immich_name": name,
                            "person_id": match["id"],
                            "display_name": match["display_name"],
                            "status": (link or {}).get("status") or "unlinked",
                        }
                    )
        report["counts"] = dict(report["counts"])
        return report

    # everything ------------------------------------------------------------
    DEFAULT_LIMITS = {
        "backfill": 200, "native": 200, "upload": 200, "pull": 300, "locate": 1000,
        "nsfw": 300, "tags": 500, "gemini": 200,
    }

    def sync(self, limits=None, sources=SOURCES):
        limits = dict(self.DEFAULT_LIMITS, **(limits or {}))
        report = {}
        try:
            self.refresh_prefs()
        except Exception as error:
            if hasattr(self.conn, "rollback"):
                self.conn.rollback()
            LOG.warning("image settings unreadable, using defaults: %s", error)
            self._prefs = dict(user_settings.DEFAULTS, available=False)

        def step(name, fn):
            started = time.monotonic()
            try:
                report[name] = dict(fn())
            except Exception as error:
                if hasattr(self.conn, "rollback"):
                    self.conn.rollback()
                report[name] = {"error": f"{type(error).__name__}: {str(error)[:200]}"}
            report[name]["seconds"] = round(time.monotonic() - started, 1)
            LOG.info("images %s: %s", name, report[name])

        for source in sources:
            step(f"backfill:{source}", lambda s=source: self.backfill(s, limit=limits["backfill"]))
        step("native", lambda: self.native(limits["native"]))
        step("upload", lambda: self.upload(limits["upload"]))
        step("pull", lambda: self.pull(limits["pull"]))
        if self.prefs.get("available") and not self.previews_reclassified():
            # Once after migration 0065: covers become previews before locate
            # can place them (and before their faces reach the people graph).
            step("reclassify_previews", lambda: {
                k: v for k, v in self.reclassify_previews().items() if not isinstance(v, dict)
            })
        if self.previews_reclassified() or not self.prefs.get("available"):
            step("locate", lambda: self.locate(limits["locate"]))
        else:
            report["locate"] = {"skipped": "waiting_for_reclassify_previews"}
        step("nsfw", lambda: self.nsfw(limits["nsfw"]))
        step("tags", lambda: self.tags(limits["tags"]))
        step("gemini", lambda: self.gemini(limits["gemini"]))
        step("native_messages", self.native_messages)
        return report


def email_requests(conn, settings):
    """The request list for images/email_fetch.py (taken attachments not yet imported)."""
    from .sources import email

    return email.requests(email.discover(conn, settings))


def beeper_requests(conn, settings, channels=None, limit=None):
    """The request list for images/beeper_fetch.py (export-routed attachments not yet imported)."""
    from .sources import beeper

    found = [
        c
        for c in beeper.discover(conn, settings)
        if c.route == "export" and c.skip in (None, "needs_beeper_export") and (not channels or c.channel in channels)
    ]
    out = beeper.requests(found)
    return out[:limit] if limit else out


__all__ = ["Pipeline", "Candidate", "photo_sessions", "describe", "face_rows", "ocr_text_of"]
