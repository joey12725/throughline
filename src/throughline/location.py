"""Track attachment and deterministic, source-separated stop detection."""

import math
import os

from .ingest import ingest, register_channel
from .models import Ingest


# Nearest usable point for message m of channel c: closest in time, then most
# accurate. Withdrawn points never qualify. Only life events are placed by the
# track; reference, page-revision and derived records never are.
NEAREST = """(SELECT p.* FROM tl.location_track p
      WHERE NOT EXISTS(SELECT 1 FROM tl.retired_track r WHERE r.track_id=p.id)
      AND p.at BETWEEN m.happened_at-make_interval(secs=>c.location_tolerance_s) AND m.happened_at+make_interval(secs=>c.location_tolerance_s)
      ORDER BY abs(extract(epoch FROM p.at-m.happened_at)),p.accuracy_m NULLS LAST,p.id LIMIT 1)"""


def attach_late(conn, window=None):
    """Place life events at their nearest track point.

    window (start, end) limits the pass to messages a point in that span can
    reach: no message farther than the largest channel tolerance from every
    new point can change. None revisits every message.
    """
    bound, params = "", ()
    if window is not None:
        reach = conn.execute(
            "SELECT coalesce(max(location_tolerance_s),0) s FROM tl.channels"
        ).fetchone()["s"]
        bound = " AND m.happened_at BETWEEN %s::timestamptz-make_interval(secs=>%s) AND %s::timestamptz+make_interval(secs=>%s)"
        params = (window[0], reach, window[1], reach)
    attached = conn.execute(f"""WITH candidates AS (
      SELECT m.id,t.id track_id,t.lat,t.lon,t.accuracy_m FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id
      CROSS JOIN LATERAL {NEAREST} t
      WHERE c.record_kind='life_event' AND m.superseded_by IS NULL AND (m.lat IS NULL OR m.loc_method='track'){bound}
    ) UPDATE tl.messages m SET lat=t.lat,lon=t.lon,loc_accuracy_m=t.accuracy_m,loc_method='track',loc_reason=NULL
      FROM candidates t WHERE m.id=t.id AND (m.lat,m.lon,m.loc_accuracy_m) IS DISTINCT FROM (t.lat,t.lon,t.accuracy_m)""", params).rowcount
    conn.execute(f"""INSERT INTO tl.message_locations(message_id,track_id)
      SELECT m.id,p.id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id
      CROSS JOIN LATERAL {NEAREST} p
      WHERE m.superseded_by IS NULL AND m.loc_method='track'{bound}
      ON CONFLICT(message_id) DO UPDATE SET track_id=EXCLUDED.track_id WHERE tl.message_locations.track_id<>EXCLUDED.track_id""", params)
    return attached


def ingest_track(conn, batch):
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO tl.location_track(at,lat,lon,accuracy_m,source) VALUES (%s,%s,%s,%s,%s) ON CONFLICT(source,at) DO NOTHING",
            [(p.at, p.lat, p.lon, p.accuracy_m, p.source) for p in batch.points],
        )
    times = [p.at for p in batch.points]
    window = (min(times), max(times)) if times else None
    return dict(received=len(batch.points), attached=attach_late(conn, window))


def retire_track(conn, source=None, ids=None, reason=None, restore=False):
    """Withdraw (or restore) track points; needs the owner database role.

    Runtime roles cannot write tl.retired_track, so no HTTP token can do this.
    Messages located by a withdrawn point move to the next nearest point within
    tolerance or lose their track location; nothing is deleted. Run stop
    detection afterwards to reconcile visits built from those points.
    """
    if (source is None) == (not ids):
        raise ValueError("give either a source or point ids")
    selected = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM tl.location_track WHERE (%s::text IS NOT NULL AND source=%s) OR id=ANY(%s::bigint[]) ORDER BY id",
            (source, source, list(ids or [])),
        )
    ]
    if restore:
        changed = conn.execute(
            "DELETE FROM tl.retired_track WHERE track_id=ANY(%s)", (selected,)
        ).rowcount
        cleared = 0
    else:
        # Versions located before provenance was recorded (and superseded
        # versions, which late attachment never revisits) have no row saying
        # which point placed them. Record it now, while the point is still
        # findable: the point within tolerance carrying the message's own
        # coordinates, else the nearest. Withdrawn points count here, so a
        # rerun repairs what an earlier withdrawal missed.
        conn.execute("""INSERT INTO tl.message_locations(message_id,track_id)
          SELECT m.id,p.id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id
          CROSS JOIN LATERAL (SELECT p.id FROM tl.location_track p
            WHERE p.at BETWEEN m.happened_at-make_interval(secs=>c.location_tolerance_s) AND m.happened_at+make_interval(secs=>c.location_tolerance_s)
            ORDER BY (p.lat=m.lat AND p.lon=m.lon) DESC,abs(extract(epoch FROM p.at-m.happened_at)),p.accuracy_m NULLS LAST,p.id LIMIT 1) p
          WHERE m.loc_method='track' AND NOT EXISTS(SELECT 1 FROM tl.message_locations ml WHERE ml.message_id=m.id)
          ON CONFLICT(message_id) DO NOTHING""")
        changed = conn.execute(
            "INSERT INTO tl.retired_track(track_id,reason) SELECT unnest(%s::bigint[]),%s ON CONFLICT DO NOTHING",
            (selected, reason),
        ).rowcount
        # Every version that was located by a withdrawn point loses it; current
        # versions are then re-attached to what remains.
        cleared = conn.execute(
            "UPDATE tl.messages m SET lat=NULL,lon=NULL,loc_accuracy_m=NULL,loc_method=NULL,loc_reason=CASE WHEN (SELECT c.record_kind FROM tl.channels c WHERE c.id=m.channel_id)='life_event' THEN 'no_track_point_within_tolerance' ELSE 'not_applicable' END FROM tl.message_locations ml WHERE ml.message_id=m.id AND ml.track_id=ANY(%s) AND m.loc_method='track'",
            (selected,),
        ).rowcount
        conn.execute(
            "DELETE FROM tl.message_locations WHERE track_id=ANY(%s)", (selected,)
        )
    return dict(
        points=len(selected),
        changed=changed,
        cleared=cleared,
        attached=attach_late(conn),
    )


def distance(a, b):
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    x = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1)
        * math.cos(lat2)
        * math.sin(math.radians(b["lon"] - a["lon"]) / 2) ** 2
    )
    return 6371000 * 2 * math.asin(min(1, math.sqrt(x)))


def max_gap_s():
    # A stationary device may report rarely (overnight, or only on movement).
    # Consecutive in-radius samples stay one stay up to this silence.
    value = float(os.getenv("THROUGHLINE_VISIT_MAX_GAP_S", "43200"))
    if value <= 0:
        raise ValueError("visit maximum gap must be positive")
    return value


def detect_visits(conn, radius_m=100, min_stay_s=600, max_gap=None):
    if radius_m <= 0 or min_stay_s <= 0:
        raise ValueError("radius and minimum stay must be positive")
    max_gap = max_gap_s() if max_gap is None else max_gap
    if max_gap <= 0:
        raise ValueError("maximum gap must be positive")
    register_channel(
        conn,
        dict(
            name="visits",
            plugin="native",
            description="Detected stays",
            fields=[],
            record_kind="derived",
        ),
    )
    # Serialize detection runs before reading: reconciliation below decides
    # from the current visits and must not interleave with another run.
    conn.execute(
        "SELECT pg_advisory_xact_lock(id) FROM tl.channels WHERE name='visits'"
    )
    groups = []
    group = []
    points = list(
        conn.execute(
            "SELECT t.* FROM tl.location_track t WHERE NOT EXISTS(SELECT 1 FROM tl.retired_track r WHERE r.track_id=t.id) ORDER BY t.source,t.at,t.id"
        )
    )
    for i, point in enumerate(points):
        if group:
            center = dict(
                lat=sum(p["lat"] for p in group) / len(group),
                lon=sum(p["lon"] for p in group) / len(group),
            )
            outside = distance(center, point) > radius_m
            # Ignore one isolated excursion when the next sample returns to the stay.
            if (
                outside
                and i + 1 < len(points)
                and points[i + 1]["source"] == point["source"] == group[0]["source"]
                and distance(center, points[i + 1]) <= radius_m
            ):
                continue
            if (
                point["source"] != group[0]["source"]
                or outside
                or (point["at"] - group[-1]["at"]).total_seconds() > max_gap
            ):
                groups.append(group)
                group = []
        group.append(point)
    if group:
        groups.append(group)
    messages = []
    for g in groups:
        if (g[-1]["at"] - g[0]["at"]).total_seconds() < min_stay_s:
            continue
        lat = sum(p["lat"] for p in g) / len(g)
        lon = sum(p["lon"] for p in g) / len(g)
        messages.append(
            dict(
                channel="visits",
                external_id=f"stop:{g[0]['source']}:{g[0]['at'].isoformat()}",
                thread={"external_id": g[0]["at"].date().isoformat()},
                happened_at=g[0]["at"].isoformat(),
                ended_at=g[-1]["at"].isoformat(),
                content="Detected visit",
                location=dict(lat=lat, lon=lon),
                payload={"track_ids": [p["id"] for p in g]},
            )
        )
    # Reconcile recomputed stays with the existing logical visits. Every visit
    # keeps one external id for life and every stay in a run gets a distinct
    # one, so ingest versions visits and never folds two stays together:
    #  1. a stay whose natural id (source + first point) already exists keeps
    #     it, reviving a retired visit if necessary;
    #  2. a remaining stay adopts the unclaimed active visit it shares the most
    #     track points with (a backfill that extends a stay makes a version);
    #  3. any other stay is new under its natural id;
    #  4. active visits nobody claimed are retired under their own id.
    existing = list(
        conn.execute(
            "SELECT m.*,p.payload FROM api.messages m JOIN tl.raw_payloads p ON p.message_id=m.id WHERE m.channel='visits' AND p.payload ? 'track_ids' ORDER BY m.happened_at,m.id"
        )
    )
    by_external_id = {r["external_id"]: r for r in existing}
    # Ids whose history has no current row cannot be versioned by ingest. Only
    # an earlier reconciliation that cross-linked visits left such rows.
    unusable = {
        r["external_id"]
        for r in conn.execute(
            "SELECT DISTINCT m.external_id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name='visits' AND NOT EXISTS(SELECT 1 FROM tl.messages cur WHERE cur.channel_id=m.channel_id AND cur.external_id=m.external_id AND cur.superseded_by IS NULL)"
        )
    }
    used = set()
    pending = []
    for message in messages:
        old = by_external_id.get(message["external_id"])
        if old:
            used.add(old["id"])
        else:
            pending.append(message)
    active = [
        r for r in existing if r["id"] not in used and not r["payload"].get("retired")
    ]
    overlaps = []
    for index, message in enumerate(pending):
        ids = set(message["payload"]["track_ids"])
        for rank, old in enumerate(active):
            shared = len(ids.intersection(old["payload"].get("track_ids", [])))
            if shared:
                overlaps.append((-shared, index, rank))
    adopted = {}
    for _, index, rank in sorted(overlaps):
        old = active[rank]
        if index not in adopted and old["id"] not in used:
            adopted[index] = old
            used.add(old["id"])
    taken = set(by_external_id) | unusable
    for index, message in enumerate(pending):
        if index in adopted:
            message["external_id"] = adopted[index]["external_id"]
            continue
        natural, n = message["external_id"], 1
        while message["external_id"] in taken:
            n += 1
            message["external_id"] = f"{natural}#{n}"
        taken.add(message["external_id"])
    retired = 0
    for old in existing:
        if old["id"] not in used and not old["payload"].get("retired"):
            messages.append(
                dict(
                    channel="visits",
                    external_id=old["external_id"],
                    thread={
                        "external_id": old["thread_external_id"],
                        "title": old["thread_title"],
                    },
                    happened_at=old["happened_at"].isoformat(),
                    ended_at=old["ended_at"].isoformat() if old["ended_at"] else None,
                    content="Detected visit retired after track reconciliation",
                    payload={"track_ids": old["payload"]["track_ids"], "retired": True},
                )
            )
            retired += 1
    result = ingest(conn, Ingest(plugin="native", messages=messages))
    result["retired"] = retired
    conn.execute(
        "UPDATE tl.links l SET status='removed' FROM api.messages m JOIN tl.raw_payloads p ON p.message_id=m.id WHERE l.from_kind='message' AND l.from_id=m.id AND l.type='at_place' AND l.method='rule' AND l.status='active' AND m.channel='visits' AND p.payload->'retired'='true'::jsonb"
    )
    conn.execute(
        """INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id,method)
      SELECT 'at_place','message',m.id,'entity',p.id,'rule'
      FROM api.visits m JOIN tl.entities p ON p.kind='place' AND p.merged_into IS NULL
      AND p.metadata ? 'lat' AND p.metadata ? 'lon'
      AND earth_distance(ll_to_earth(m.lat,m.lon),ll_to_earth((p.metadata->>'lat')::float8,(p.metadata->>'lon')::float8))<=%s
      WHERE NOT EXISTS (SELECT 1 FROM tl.links l WHERE l.type='at_place' AND l.from_kind='message' AND l.from_id=m.id AND l.to_kind='entity' AND l.to_id=p.id AND l.status='active')""",
        (radius_m,),
    )
    return result
