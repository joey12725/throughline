"""Images: metadata, bytes, inline data and image search (docs/images.md).

Every asset route reads through the reader path under the token's scope: an
asset is visible when one of its messages is (api.assets), so a hidden asset
and a missing one get the same 404, and nothing is fetched from Immich before
that check. Bytes are proxied from Immich with the server's key (IMMICH_URL,
IMMICH_API_KEY); the key never reaches the caller.

The people routes (photographed-with, nearby) name people, so they are for
unrestricted askers only.

NSFW follows the owner's image settings (tl.image_settings, migration 0065;
GET/PUT /v1/images/settings, owner tokens only). An asset that is not safe
(api.assets.safe: scored at or above nsfw_threshold, or not scored yet under
unscanned=hide, or unscored and tagged nsfw), under the asker's mode
(nsfw_mode, or restricted_nsfw_mode for a restricted asker):
- hide: 404 on the metadata, thumb and content routes and left out of inline
  and search, unless include_nsfw;
- blur: listed with nsfw true; thumb, content and inline serve a heavily
  blurred image (Gaussian, radius 8% of the long side) unless include_nsfw;
- show: served as is (nsfw stays true on the card).
Shared-post covers (role preview) are left out of search under
shared_previews=exclude. Before 0065 the defaults apply (hide, 0.6).

Browsers: the web client authenticates with a bearer header, which an
<img src> cannot send. It fetches thumb/content with the header and shows a
blob: URL (or uses POST /v1/assets/inline); there are no signed URLs.
"""

import base64
import io
import logging
import os
from datetime import datetime

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ... import writes
from ...images import user_settings
from ...providers import ProviderUnavailable
from ...providers import visual as visual_models
from ...retrieval import images
from ...retrieval.scope_sql import lit
from ..deps import (
    Token,
    audit,
    authenticated_connection,
    is_unrestricted,
    require,
    require_unrestricted,
    scope_of,
)

router = APIRouter()
log = logging.getLogger("throughline.api.assets")

# Tests replace this with an httpx.MockTransport.
transport = None
IMMICH_TIMEOUT_S = 30
# Immich's preview is a JPEG of at most this many pixels on the long side
# (its default thumbnail settings).
PREVIEW_PX = 1440
SAFE_MIMES = ("image/jpeg", "image/png", "image/webp", "image/gif")
INLINE_MAX_ASSETS = 8
INLINE_MAX_BYTES = 4 * 1024 * 1024
RRF_K = 60
# Blur radius of an unsafe image under nsfw_mode=blur, as a share of its long side.
BLUR_SHARE = 0.08


# --- Immich ----------------------------------------------------------------------


class ImmichUnavailable(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _immich_get(path, params=None):
    """(bytes, content type) of an Immich GET with the server's key."""
    base = (os.getenv("IMMICH_URL") or "http://127.0.0.1:2283").rstrip("/")
    key = os.getenv("IMMICH_API_KEY")
    if not key:
        raise ImmichUnavailable(503, "image store not configured (IMMICH_API_KEY)")
    try:
        with httpx.Client(
            timeout=IMMICH_TIMEOUT_S,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        ) as client:
            response = client.get(
                base + path, params=params, headers={"x-api-key": key}
            )
    except httpx.HTTPError:
        raise ImmichUnavailable(502, "image store unreachable") from None
    if response.status_code == 404:
        raise ImmichUnavailable(404, "asset not found")
    if response.status_code != 200:
        raise ImmichUnavailable(502, f"image store answered HTTP {response.status_code}")
    ctype = response.headers.get("content-type", "application/octet-stream")
    return response.content, ctype.split(";")[0].strip()


def _thumbnail(immich_id, size):
    return _immich_get(f"/api/assets/{immich_id}/thumbnail", {"size": size})


def _original(immich_id):
    return _immich_get(f"/api/assets/{immich_id}/original")


def _pil():
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None, None
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except Exception:
        pass
    return Image, ImageOps


def _downscale(data, ctype, max_px):
    """(bytes, content type) no larger than max_px on the long side. Without
    Pillow, or for bytes it cannot read, the input is returned (ok False)."""
    Image, ImageOps = _pil()
    if Image is None:
        return data, ctype, False
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            if max(img.size) <= max_px and ctype in SAFE_MIMES:
                return data, ctype, True
            img.thumbnail((max_px, max_px))
            out = io.BytesIO()
            if img.mode in ("RGBA", "LA", "P") and ctype == "image/png":
                img.save(out, "PNG", optimize=True)
                return out.getvalue(), "image/png", True
            img.convert("RGB").save(out, "JPEG", quality=85)
            return out.getvalue(), "image/jpeg", True
    except Exception:
        return data, ctype, False


def _content(asset, max_px):
    """The asset's image at most max_px on the long side: the original when
    it already fits and a browser shows it, else a downscaled original or
    Immich's preview (videos, HEIC without pillow-heif)."""
    immich_id = asset["immich_id"]
    mime = (asset.get("mime") or "").lower()
    long_side = max(asset.get("width") or 0, asset.get("height") or 0)
    if asset.get("kind") == "video" or mime.startswith("video/"):
        data, ctype = _thumbnail(immich_id, "preview")
        return _downscale(data, ctype, max_px)[:2]
    if long_side and long_side <= max_px and mime in SAFE_MIMES:
        return _original(immich_id)
    if long_side and long_side > max_px and max_px <= PREVIEW_PX:
        data, ctype = _thumbnail(immich_id, "preview")
        return _downscale(data, ctype, max_px)[:2]
    data, ctype = _original(immich_id)
    small, small_type, ok = _downscale(data, mime or ctype, max_px)
    if ok:
        return small, small_type
    data, ctype = _thumbnail(immich_id, "preview")
    return _downscale(data, ctype, max_px)[:2]


def _blur(data):
    """(JPEG bytes, 'image/jpeg') heavily blurred, or None without Pillow or
    for bytes it cannot read (the caller then serves nothing)."""
    Image, ImageOps = _pil()
    if Image is None:
        return None
    try:
        from PIL import ImageFilter

        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            radius = max(2.0, max(img.size) * BLUR_SHARE)
            out = io.BytesIO()
            img.filter(ImageFilter.GaussianBlur(radius)).save(out, "JPEG", quality=70)
            return out.getvalue(), "image/jpeg"
    except Exception:
        return None


# --- settings and policy -------------------------------------------------------------


def _policy(token):
    """The image settings as they apply to this request: mode (the asker's
    nsfw mode) and the settings (DEFAULTS before 0065)."""
    prefs = user_settings.read()
    unrestricted = is_unrestricted(token)
    return dict(prefs, mode=user_settings.mode_for(prefs, unrestricted), unrestricted=unrestricted)


def _treatment(row, policy, include_nsfw):
    """How a visible asset is served: 'plain', 'blur' or None (404)."""
    if row is None:
        return None
    if row.get("safe") or include_nsfw or policy["mode"] == "show":
        return "plain"
    return "blur" if policy["mode"] == "blur" else None


# --- reads --------------------------------------------------------------------------

ASSET_COLUMNS = (
    "a.id,a.immich_id,a.mime,a.bytes,a.width,a.height,a.duration_s,a.kind,a.tags,"
    "a.ocr_text,a.caption,a.taken_at,a.lat,a.lon,a.place,a.nsfw,a.nsfw_labels,"
    "a.origin,a.original_name,a.status,a.safe"
)
# api.assets columns of migration 0065.
ASSET_COLUMNS_0065 = ",a.preview,a.loc_method,a.loc_accuracy_m"


def _run(scope, token):
    return images.reader(scope, token)


def _columns(policy):
    return ASSET_COLUMNS + (ASSET_COLUMNS_0065 if policy.get("available") else "")


def _visible_assets(run, ids, policy=None):
    ids = sorted({int(i) for i in ids})
    if not ids:
        return {}
    rows = run(
        f"SELECT {_columns(policy or {})} FROM api.assets a WHERE a.id = ANY({lit(ids, 'bigint[]')})",
        len(ids),
    )
    return {r["id"]: r for r in rows}


def _asset_or_404(scope, token, asset_id, include_nsfw=False):
    """(row, 'plain' | 'blur') of a visible asset this request may see."""
    policy = _policy(token)
    found = _visible_assets(_run(scope, token), [asset_id], policy).get(asset_id)
    how = _treatment(found, policy, include_nsfw)
    if how is None:
        raise HTTPException(404, "asset not found")
    return found, how


def _people(run, ids):
    """{asset_id: [{person_id, name}]} (unrestricted askers; empty otherwise)."""
    ids = sorted({int(i) for i in ids})
    if not ids:
        return {}
    rows = run(
        "SELECT DISTINCT f.asset_id, f.person_id, f.name FROM api.asset_faces f"
        f" WHERE f.asset_id = ANY({lit(ids, 'bigint[]')}) AND f.person_id IS NOT NULL"
        " ORDER BY f.asset_id, f.name, f.person_id",
        len(ids) * 50,
    )
    out = {}
    for r in rows:
        out.setdefault(r["asset_id"], []).append(
            dict(person_id=r["person_id"], name=r["name"])
        )
    return out


def _messages(run, ids, per_asset=20):
    ids = sorted({int(i) for i in ids})
    if not ids:
        return {}
    rows = run(
        "SELECT ma.asset_id, ma.message_id, ma.channel, ma.external_id, ma.role, ma.ord"
        f" FROM api.message_assets ma WHERE ma.asset_id = ANY({lit(ids, 'bigint[]')})"
        " ORDER BY ma.asset_id, ma.message_id",
        len(ids) * per_asset,
    )
    out = {}
    for r in rows:
        items = out.setdefault(r["asset_id"], [])
        if len(items) < per_asset:
            items.append(
                dict(
                    message_id=r["message_id"],
                    channel=r["channel"],
                    external_id=r["external_id"],
                    role=r["role"],
                )
            )
    return out


def _card(row, people, messages, blurred=False):
    media = row.get("immich_id") is not None
    card = images.image(
        dict(
            asset_id=row["id"],
            has_media=media,
            nsfw=not row.get("safe"),
            blurred=blurred,
            location_source=row.get("loc_method"),
            kind=row.get("kind"),
            tags=row.get("tags"),
            people=people,
            place=row.get("place"),
            taken_at=row.get("taken_at"),
            ocr=(row.get("ocr_text") or "")[:300] or None,
            caption=row.get("caption"),
            width=row.get("width"),
            height=row.get("height"),
        )
    )
    card["messages"] = messages
    return card


def _cards(run, rows, policy=None, include_nsfw=False):
    ids = [r["id"] for r in rows]
    people, messages = _people(run, ids), _messages(run, ids, per_asset=5)
    return [
        _card(
            r,
            people.get(r["id"], []),
            messages.get(r["id"], []),
            blurred=_treatment(r, policy or {"mode": "hide"}, include_nsfw) == "blur",
        )
        for r in rows
    ]


@router.get("/v1/assets/{asset_id}")
def asset(asset_id: int, token: Token, include_nsfw: bool = False):
    """One asset's metadata, its messages and (unrestricted) its people.
    nsfw is the flag (not safe); nsfw_score the NudeNet score."""
    require(token, "owner", "agent")
    scope = scope_of(token)
    run = _run(scope, token)
    row, how = _asset_or_404(scope, token, asset_id, include_nsfw)
    card = _card(
        row,
        _people(run, [asset_id]).get(asset_id, []),
        _messages(run, [asset_id]).get(asset_id, []),
        blurred=how == "blur",
    )
    card.update(
        ocr=row.get("ocr_text"),
        mime=row.get("mime"),
        bytes=row.get("bytes"),
        duration_s=row.get("duration_s"),
        lat=row.get("lat"),
        lon=row.get("lon"),
        loc_accuracy_m=row.get("loc_accuracy_m"),
        nsfw_score=row.get("nsfw"),
        nsfw_labels=row.get("nsfw_labels"),
        origin=row.get("origin"),
        original_name=row.get("original_name"),
        status=row.get("status"),
    )
    audit(token, 1, operation="asset", asset_id=asset_id)
    return card


def _bytes_response(data, ctype):
    return Response(
        content=data,
        media_type=ctype,
        headers={"Cache-Control": "private, max-age=3600"},
    )


def _media(found):
    row, how = found
    if row.get("immich_id") is None:
        raise HTTPException(404, "asset bytes not available yet")
    return row, how


def _served(data, ctype, how):
    """The bytes as this request may see them (blurred under nsfw_mode=blur)."""
    if how != "blur":
        return data, ctype
    blurred = _blur(data)
    if blurred is None:
        raise HTTPException(404, "asset not found")
    return blurred


@router.get("/v1/assets/{asset_id}/thumb")
def asset_thumb(asset_id: int, token: Token, include_nsfw: bool = False):
    """Immich's small thumbnail of a visible asset (bytes)."""
    require(token, "owner", "agent")
    row, how = _media(_asset_or_404(scope_of(token), token, asset_id, include_nsfw))
    try:
        data, ctype = _thumbnail(row["immich_id"], "thumbnail")
    except ImmichUnavailable as exc:
        raise HTTPException(exc.status, exc.detail) from None
    data, ctype = _served(data, ctype, how)
    audit(token, 1, operation="asset_thumb", asset_id=asset_id, blurred=how == "blur")
    return _bytes_response(data, ctype)


@router.get("/v1/assets/{asset_id}/content")
def asset_content(
    asset_id: int,
    token: Token,
    max_px: int = Query(default=1536, ge=64, le=8192),
    include_nsfw: bool = False,
):
    """The image of a visible asset, at most max_px on the long side (bytes)."""
    require(token, "owner", "agent")
    row, how = _media(_asset_or_404(scope_of(token), token, asset_id, include_nsfw))
    try:
        data, ctype = _content(row, max_px)
    except ImmichUnavailable as exc:
        raise HTTPException(exc.status, exc.detail) from None
    data, ctype = _served(data, ctype, how)
    audit(token, 1, operation="asset_content", asset_id=asset_id, max_px=max_px, blurred=how == "blur")
    return _bytes_response(data, ctype)


class InlineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_ids: list[int] = Field(min_length=1, max_length=INLINE_MAX_ASSETS)
    max_px: int = Field(default=1024, ge=64, le=2048)
    include_nsfw: bool = False


@router.post("/v1/assets/inline")
def assets_inline(data: InlineRequest, token: Token):
    """Base64 data URIs of up to 8 visible assets (about 4 MB in total) for
    multimodal tool calls. Assets left out are listed with the reason."""
    require(token, "owner", "agent")
    policy = _policy(token)
    found = _visible_assets(_run(scope_of(token), token), data.asset_ids, policy)
    out, skipped, total = [], [], 0
    for asset_id in dict.fromkeys(data.asset_ids):
        row = found.get(asset_id)
        if row is None:
            skipped.append(dict(asset_id=asset_id, reason="not_found"))
            continue
        how = _treatment(row, policy, data.include_nsfw)
        if how is None:
            skipped.append(dict(asset_id=asset_id, reason="nsfw"))
            continue
        if row.get("immich_id") is None:
            skipped.append(dict(asset_id=asset_id, reason="not_uploaded"))
            continue
        try:
            body, ctype = _content(row, data.max_px)
        except ImmichUnavailable as exc:
            skipped.append(dict(asset_id=asset_id, reason=f"image_store:{exc.status}"))
            continue
        if how == "blur":
            blurred = _blur(body)
            if blurred is None:
                skipped.append(dict(asset_id=asset_id, reason="nsfw"))
                continue
            body, ctype = blurred
        encoded = base64.b64encode(body).decode("ascii")
        if total + len(encoded) > INLINE_MAX_BYTES:
            skipped.append(dict(asset_id=asset_id, reason="size_cap"))
            continue
        total += len(encoded)
        item = dict(
            asset_id=asset_id,
            mime=ctype,
            bytes=len(body),
            data_uri=f"data:{ctype};base64,{encoded}",
        )
        if how == "blur":
            item["blurred"] = True
        out.append(item)
    audit(token, len(out), operation="assets_inline")
    result = dict(images=out, max_px=data.max_px)
    if skipped:
        result["skipped"] = skipped
    return result


def images_nsfw(row):
    """Not safe (api.assets.safe): scored at or above the threshold, not
    scored yet (unscanned=hide), or unscored and tagged nsfw."""
    return not row.get("safe")


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str | None = Field(default=None, max_length=1000)
    kind: list[str] | None = None
    tags: list[str] | None = None
    persons: list[int] | None = None
    place: str | None = Field(default=None, max_length=200)
    after: datetime | None = None
    before: datetime | None = None
    channels: list[str] | None = None
    include_nsfw: bool = False
    limit: int = Field(default=24, ge=1, le=200)

    @field_validator("kind", "tags", "channels", mode="before")
    @classmethod
    def listify(cls, value):
        if isinstance(value, str):
            return [value]
        return value

    @field_validator("after", "before", mode="before")
    @classmethod
    def date_only(cls, value):
        if isinstance(value, str) and len(value.strip()) == 10:
            return value.strip() + "T00:00:00+00:00"
        return value


def _filters(data, policy=None):
    """(user filters, policy filters) as SQL conditions on api.assets a."""
    policy = policy or {"mode": "hide"}
    where = []
    if data.kind:
        where.append(f"a.kind = ANY({lit(list(data.kind), 'text[]')})")
    if data.tags:
        from ...images.store import normalize_tags

        where.append(f"a.tags @> {lit(normalize_tags(data.tags), 'text[]')}")
    if data.place:
        value = data.place.strip().lower()
        where.append(
            "(lower(a.place->>'city') = {v} OR lower(a.place->>'state') = {v}"
            " OR lower(a.place->>'country') = {v})".format(v=lit(value))
        )
    if data.after:
        where.append(f"a.taken_at >= {lit(data.after.isoformat())}::timestamptz")
    if data.before:
        where.append(f"a.taken_at < {lit(data.before.isoformat())}::timestamptz")
    if data.channels:
        where.append(
            "EXISTS(SELECT 1 FROM api.message_assets ma WHERE ma.asset_id = a.id"
            f" AND ma.channel = ANY({lit(list(data.channels), 'text[]')}))"
        )
    if data.persons:
        where.append(
            "EXISTS(SELECT 1 FROM api.asset_faces f WHERE f.asset_id = a.id"
            f" AND f.person_id = ANY({lit([int(p) for p in data.persons], 'bigint[]')}))"
        )
    rules = []
    if not data.include_nsfw and policy["mode"] == "hide":
        rules.append("a.safe")
    if policy.get("available") and policy.get("shared_previews") == "exclude":
        rules.append("NOT a.preview")
    return where, rules


@router.post("/v1/assets/search")
def assets_search(data: SearchRequest, token: Token):
    """Images only: Gemini and SigLIP vector legs plus keywords over the
    images' search text, fused with RRF, within the filters (kind, tags,
    persons, place, time, channels, nsfw). Without a query: newest first."""
    require(token, "owner", "agent")
    scope = scope_of(token)
    run = _run(scope, token)
    policy = _policy(token)
    user_where, rules = _filters(data, policy)
    where = user_where + rules
    query = (data.query or "").strip()
    searched, skipped = {}, {}
    prefilter = None
    has_filter = bool(user_where)
    if not query or has_filter:
        cap = data.limit if not query else 20000
        rows = run(
            f"SELECT a.id FROM api.assets a WHERE {' AND '.join(where) or 'true'}"
            f" ORDER BY a.taken_at DESC NULLS LAST, a.id DESC LIMIT {int(cap)}",
            cap,
        )
        prefilter = [r["id"] for r in rows]
        if not query:
            ranked = prefilter
            searched["filters"] = len(ranked)
        elif not prefilter:
            ranked = []
    if query and (prefilter is None or prefilter):
        pool = min(500, max(data.limit * 4, 50))
        ids_arg = lit(prefilter, "bigint[]") if prefilter is not None else "NULL::bigint[]"
        lists = []
        for which, model in (
            ("gemini", visual_models.gemini_model()),
            ("siglip", visual_models.SIGLIP),
        ):
            try:
                vector, used = visual_models.embed_query(query, which, scope=scope)
            except ProviderUnavailable as exc:
                skipped[which] = str(exc)[:160]
                continue
            hits = run(
                "SELECT * FROM api.visual_assets("
                f"{lit('[' + ','.join(str(float(x)) for x in vector) + ']')},{lit(used)},"
                f"{pool},{lit(data.include_nsfw)},{ids_arg})",
                pool,
            )
            searched[which] = len(hits)
            if hits:
                lists.append([h["asset_id"] for h in hits])
        hits = run(
            f"SELECT * FROM api.image_search_assets({lit(query)},{pool},"
            f"{lit(data.include_nsfw)},{ids_arg})",
            pool,
        )
        searched["lexical"] = len(hits)
        if hits:
            lists.append([h["asset_id"] for h in hits])
        ranked = _fuse(lists)
    ranked = ranked[: data.limit]
    found = _visible_assets(run, ranked, policy)
    rows = [found[i] for i in ranked if i in found]
    result = dict(images=_cards(run, rows, policy, data.include_nsfw), searched=searched)
    if skipped:
        result["skipped_legs"] = skipped
    audit(token, len(rows), operation="assets_search")
    return result


def _fuse(lists):
    """Reciprocal rank fusion (k=60) of ranked id lists; ties by first seen."""
    scores, order = {}, []
    for ranking in lists:
        for rank, item in enumerate(ranking, start=1):
            if item not in scores:
                order.append(item)
            scores[item] = scores.get(item, 0.0) + 1.0 / (RRF_K + rank)
    first = {item: index for index, item in enumerate(order)}
    return sorted(order, key=lambda i: (-scores[i], first[i]))


# --- people -----------------------------------------------------------------------------

NSFW_SQL = "(tl.asset_safe({a}.nsfw,{a}.kind,{a}.enrich) AND NOT 'nsfw'=ANY({a}.tags))"
# A shared-post cover (every link a preview) is not a photo anyone took.
NOT_PREVIEW_SQL = (
    "EXISTS(SELECT 1 FROM tl.message_assets px WHERE px.asset_id={a}.id AND px.role<>'preview')"
)


def _people_filter(alias, include_nsfw, policy):
    """The asset condition of the people routes: not a shared-post cover,
    and nsfw by the owner's mode (these routes are unrestricted-only)."""
    rule = NOT_PREVIEW_SQL.format(a=alias)
    if include_nsfw or policy["mode"] != "hide":
        return rule
    if not policy.get("available"):
        return f"{rule} AND {NSFW_SQL.format(a=alias)}"
    clean = "tl.asset_clean({a}.nsfw,{a}.kind,{a}.enrich,{a}.tags,{t}::real,{u})".format(
        a=alias, t=lit(float(policy["nsfw_threshold"])), u=lit(policy["unscanned"])
    )
    return f"{rule} AND {clean}"


def _person_or_404(conn, person_id):
    if not conn.execute(
        "SELECT 1 FROM tl.people WHERE id=%s AND merged_into IS NULL", (person_id,)
    ).fetchone():
        raise HTTPException(404, "person not found")


@router.get("/v1/people/{person_id}/photographed-with")
def photographed_with(
    person_id: int,
    token: Token,
    limit: int = Query(default=25, ge=1, le=200),
    include_nsfw: bool = False,
):
    """People in the same frame as this person: counts and example asset ids."""
    require(token, "owner", "agent")
    require_unrestricted(token)
    policy = _policy(token)
    nsfw = _people_filter("a", include_nsfw, policy)
    with authenticated_connection(token) as conn:
        _person_or_404(conn, person_id)
        rows = conn.execute(
            "WITH mine AS (SELECT DISTINCT f.asset_id FROM tl.asset_faces f"
            " JOIN tl.identifiers i ON i.id=f.identifier_id AND i.status='confirmed'"
            f" JOIN tl.assets a ON a.id=f.asset_id WHERE i.person_id=%(p)s AND {nsfw})"
            " SELECT p.id AS person_id,p.display_name AS name,count(DISTINCT f.asset_id) AS assets,"
            " (array_agg(DISTINCT f.asset_id ORDER BY f.asset_id DESC))[1:5] AS example_asset_ids"
            " FROM mine m JOIN tl.asset_faces f ON f.asset_id=m.asset_id"
            " JOIN tl.identifiers i ON i.id=f.identifier_id AND i.status='confirmed'"
            " JOIN tl.people p ON p.id=i.person_id AND p.merged_into IS NULL"
            " WHERE p.id<>%(p)s GROUP BY p.id,p.display_name"
            " ORDER BY assets DESC,p.id LIMIT %(n)s",
            dict(p=person_id, n=limit),
        ).fetchall()
    audit(token, len(rows), operation="photographed_with", person_id=person_id)
    return dict(person_id=person_id, people=rows)


@router.get("/v1/people/{person_id}/nearby")
def photographed_nearby(
    person_id: int,
    token: Token,
    limit: int = Query(default=25, ge=1, le=200),
    hours: float = Query(default=2, gt=0, le=48),
    meters: float = Query(default=1000, gt=0, le=50000),
    include_nsfw: bool = False,
):
    """People photographed within ±hours and meters of this person's photos
    without sharing the frame: counts and example asset ids (theirs)."""
    require(token, "owner", "agent")
    require_unrestricted(token)
    policy = _policy(token)
    nsfw_a = _people_filter("a", include_nsfw, policy)
    nsfw_q = _people_filter("q", include_nsfw, policy)
    with authenticated_connection(token) as conn:
        _person_or_404(conn, person_id)
        rows = conn.execute(
            "WITH mine AS MATERIALIZED (SELECT DISTINCT a.id,a.taken_at,a.lat,a.lon FROM tl.assets a"
            " JOIN tl.asset_faces f ON f.asset_id=a.id"
            " JOIN tl.identifiers i ON i.id=f.identifier_id AND i.status='confirmed'"
            " WHERE i.person_id=%(p)s AND a.taken_at IS NOT NULL AND a.lat IS NOT NULL AND a.lon IS NOT NULL"
            f" AND {nsfw_a}),"
            " near AS MATERIALIZED (SELECT DISTINCT m.id AS mine_id,q.id AS q_id FROM mine m"
            " JOIN tl.assets q ON q.id<>m.id AND q.lat IS NOT NULL AND q.lon IS NOT NULL"
            "  AND q.taken_at BETWEEN m.taken_at-make_interval(secs=>%(s)s) AND m.taken_at+make_interval(secs=>%(s)s)"
            "  AND public.earth_box(api.earth_point(m.lat,m.lon),%(d)s) @> api.earth_point(q.lat,q.lon)"
            "  AND public.earth_distance(api.earth_point(m.lat,m.lon),api.earth_point(q.lat,q.lon))<=%(d)s"
            f" WHERE {nsfw_q} AND NOT EXISTS(SELECT 1 FROM tl.asset_faces f2"
            "  JOIN tl.identifiers i2 ON i2.id=f2.identifier_id AND i2.status='confirmed'"
            "  WHERE f2.asset_id=q.id AND i2.person_id=%(p)s)),"
            " seen AS (SELECT n.q_id,p.id AS pid,p.display_name FROM near n"
            " JOIN tl.asset_faces f ON f.asset_id=n.q_id"
            " JOIN tl.identifiers i ON i.id=f.identifier_id AND i.status='confirmed'"
            " JOIN tl.people p ON p.id=i.person_id AND p.merged_into IS NULL"
            " WHERE p.id<>%(p)s AND NOT EXISTS(SELECT 1 FROM tl.asset_faces f3"
            "  JOIN tl.identifiers i3 ON i3.id=f3.identifier_id AND i3.status='confirmed'"
            "  WHERE f3.asset_id=n.mine_id AND i3.person_id=p.id))"
            " SELECT pid AS person_id,display_name AS name,count(DISTINCT q_id) AS assets,"
            " (array_agg(DISTINCT q_id ORDER BY q_id DESC))[1:5] AS example_asset_ids"
            " FROM seen GROUP BY pid,display_name ORDER BY assets DESC,pid LIMIT %(n)s",
            dict(p=person_id, n=limit, s=hours * 3600, d=meters),
        ).fetchall()
    audit(token, len(rows), operation="photographed_nearby", person_id=person_id)
    return dict(person_id=person_id, hours=hours, meters=meters, people=rows)


# --- settings -----------------------------------------------------------------------------


def _settings_hash(row):
    import hashlib
    import json

    text = json.dumps(jsonable_encoder(dict(row)), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def _settings_receipt(conn, _ident):
    if not user_settings.available(conn):
        return None
    row = user_settings.read(conn)
    return dict(
        record_id="image_settings:1",
        status="current",
        stored_hash=_settings_hash(user_settings.public(row)),
        written_at=row.get("updated_at"),
    )


writes.register_receipt("image_settings", _settings_receipt)


@router.get("/v1/images/settings")
def image_settings_get(token: Token):
    """The owner's image settings (owner tokens only): nsfw_mode,
    nsfw_threshold, unscanned, restricted_nsfw_mode, shared_previews,
    location_fallback, location_window_min, location_max_accuracy_m,
    updated_at; available is false before migration 0065 (defaults)."""
    require(token, "owner")
    require_unrestricted(token)
    prefs = user_settings.read()
    return dict(user_settings.public(prefs), available=prefs["available"])


@router.put("/v1/images/settings")
def image_settings_put(data: dict, token: Token, request: Request):
    """Change some image settings (owner tokens only); the body names only
    the fields to change. Answers the settings after, with a receipt when
    the request has an Idempotency-Key. 400 for an unknown field or a bad
    value, 409 before migration 0065."""
    require(token, "owner")
    require_unrestricted(token)
    if not isinstance(data, dict):
        raise HTTPException(422, "body must be an object")
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                audit(token, 0, replayed=True)
                return slot.response
            try:
                prefs = user_settings.write(conn, True, data)
            except ValueError as error:
                raise HTTPException(400, str(error)) from None
            except LookupError as error:
                raise HTTPException(409, str(error)) from None
            result = jsonable_encoder(dict(user_settings.public(prefs), available=True))
            audit(token, 1, operation="image_settings", changed=sorted(data))
            if slot.key is not None:
                result = dict(result, receipt=jsonable_encoder(writes.receipt(conn, "image_settings", 1)))
            slot.store(200, result)
    return result
