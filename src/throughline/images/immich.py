"""Immich v3 client: the server API, the machine-learning /predict endpoint and
read-only queries of Immich's own Postgres (SigLIP vectors, per-asset job status).

Endpoints used (Immich v3.4.2, /api prefix):
    POST   /assets                       upload (multipart; status created|duplicate + id)
    POST   /assets/bulk-upload-check     duplicate check by sha1 before uploading
    GET    /assets/{id}                  asset info with exifInfo, people (faces), tags
    PUT    /assets/{id}                  description
    DELETE /assets                       delete (force=true skips the trash)
    GET    /assets/{id}/thumbnail        preview bytes (size=preview|thumbnail)
    GET    /assets/{id}/original         original bytes
    GET    /assets/{id}/ocr              OCR boxes and text
    GET    /faces?id=                    faces of an asset (bbox, person)
    POST   /search/metadata              list assets (paged), for Immich-native discovery
    GET    /queues                       job queues (active/waiting counts)
    GET    /people, PUT /people/{id}     person names
    GET    /albums, POST /albums, PUT /albums/{id}/assets
    PUT    /tags (upsert by value), PUT /tags/assets (bulk tag)
v3.4.2 has no deviceId/deviceAssetId on upload (they were removed); the
pipeline marks its uploads with asset metadata key `throughline` instead.
"""

import hashlib
import json
import time
from datetime import UTC, datetime

import httpx


class ImmichError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _iso(at):
    if at is None:
        at = datetime.now(UTC)
    if at.tzinfo is None:
        at = at.astimezone()
    return at.isoformat()


METADATA_KEY = "throughline"


class Immich:
    def __init__(self, base_url, api_key, transport=None, timeout=120):
        if not base_url or not api_key:
            raise ImmichError("IMMICH_URL and IMMICH_API_KEY are required")
        self.http = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/",
            headers={"x-api-key": api_key, "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )
        self._albums = None
        self._tags = {}

    def close(self):
        self.http.close()

    def _call(self, method, path, *, retries=3, **kwargs):
        for attempt in range(retries + 1):
            try:
                response = self.http.request(method, path, **kwargs)
            except httpx.TransportError as error:
                if attempt == retries:
                    raise ImmichError(f"Immich transport failed: {type(error).__name__}") from None
                time.sleep(min(2**attempt, 8))
                continue
            if response.status_code in (429, 502, 503, 504) and attempt < retries:
                time.sleep(min(2**attempt, 8))
                continue
            if response.is_error:
                raise ImmichError(
                    f"Immich {method} {path} failed (HTTP {response.status_code}): {response.text[:200]}",
                    response.status_code,
                )
            return response
        raise ImmichError("unreachable")

    def json(self, method, path, **kwargs):
        response = self._call(method, path, **kwargs)
        return response.json() if response.content else None

    # assets ---------------------------------------------------------------
    def check_duplicate(self, sha1_hex):
        """Existing asset id for this sha1, or None."""
        import base64

        checksum = base64.b64encode(bytes.fromhex(sha1_hex)).decode()
        result = self.json(
            "POST",
            "assets/bulk-upload-check",
            json={"assets": [{"id": "q", "checksum": checksum}]},
        )
        for item in (result or {}).get("results", []):
            if item.get("action") == "reject" and item.get("assetId") and not item.get("isTrashed"):
                return item["assetId"]
        return None

    def upload(self, data, filename, *, created_at=None, modified_at=None, metadata=None):
        """(asset id, 'created'|'duplicate')."""
        sha1 = hashlib.sha1(data).hexdigest()
        form = {
            "fileCreatedAt": _iso(created_at),
            "fileModifiedAt": _iso(modified_at or created_at),
            "filename": filename,
        }
        if metadata:
            form["metadata"] = json.dumps(
                [{"key": METADATA_KEY, "value": metadata}], separators=(",", ":")
            )
        try:
            result = self.json(
                "POST",
                "assets",
                data=form,
                files={"assetData": (filename, data, "application/octet-stream")},
                headers={"x-immich-checksum": sha1},
            )
        except ImmichError as error:
            if metadata and error.status == 400 and "metadata" in str(error):
                form.pop("metadata", None)
                result = self.json(
                    "POST",
                    "assets",
                    data=form,
                    files={"assetData": (filename, data, "application/octet-stream")},
                    headers={"x-immich-checksum": sha1},
                )
            else:
                raise
        return result["id"], result.get("status", "created")

    def asset(self, asset_id):
        return self.json("GET", f"assets/{asset_id}")

    def asset_metadata(self, asset_id):
        try:
            return self.json("GET", f"assets/{asset_id}/metadata") or []
        except ImmichError:
            return []

    def update(self, asset_id, **fields):
        return self.json("PUT", f"assets/{asset_id}", json=fields)

    def delete(self, ids, force=True):
        return self._call("DELETE", "assets", json={"ids": list(ids), "force": force})

    def thumbnail(self, asset_id, size="preview"):
        return self._call("GET", f"assets/{asset_id}/thumbnail", params={"size": size}).content

    def original(self, asset_id):
        return self._call("GET", f"assets/{asset_id}/original").content

    def ocr(self, asset_id):
        return self.json("GET", f"assets/{asset_id}/ocr") or []

    def faces(self, asset_id):
        return self.json("GET", "faces", params={"id": asset_id}) or []

    def search(self, size=500, **filters):
        """Every asset matching the filters, page by page."""
        page = 1
        while True:
            body = dict(filters, page=page, size=size, withExif=True)
            result = self.json("POST", "search/metadata", json=body)
            assets = (result or {}).get("assets", {})
            yield from assets.get("items", [])
            nxt = assets.get("nextPage")
            if not nxt:
                return
            page = int(nxt)

    def queues(self):
        rows = self.json("GET", "queues") or []
        return {
            row["name"]: (row.get("statistics") or {}) for row in rows if isinstance(row, dict)
        }

    ML_QUEUES = ("metadataExtraction", "thumbnailGeneration", "smartSearch", "faceDetection", "facialRecognition", "ocr")

    def ml_idle(self):
        stats = self.queues()
        return all(
            (stats.get(q, {}).get("active", 0) or 0) + (stats.get(q, {}).get("waiting", 0) or 0) == 0
            for q in self.ML_QUEUES
        )

    # people ---------------------------------------------------------------
    def people(self):
        page = 1
        while True:
            result = self.json("GET", "people", params={"page": page, "size": 500, "withHidden": "true"})
            yield from (result or {}).get("people", [])
            if not (result or {}).get("hasNextPage"):
                return
            page += 1

    def rename_person(self, person_id, name):
        return self.json("PUT", f"people/{person_id}", json={"name": name})

    # albums and tags -------------------------------------------------------
    def album_id(self, name):
        if self._albums is None:
            self._albums = {a["albumName"]: a["id"] for a in self.json("GET", "albums") or []}
        if name not in self._albums:
            created = self.json("POST", "albums", json={"albumName": name})
            self._albums[name] = created["id"]
        return self._albums[name]

    def add_to_album(self, name, asset_ids):
        if not asset_ids:
            return
        self.json("PUT", f"albums/{self.album_id(name)}/assets", json={"ids": list(asset_ids)})

    def tag_ids(self, values):
        missing = [v for v in values if v not in self._tags]
        if missing:
            for tag in self.json("PUT", "tags", json={"tags": missing}) or []:
                self._tags[tag.get("value") or tag.get("name")] = tag["id"]
        return [self._tags[v] for v in values if v in self._tags]

    def untag(self, asset_ids, values):
        """Remove these tag values from the assets (tags never applied are ignored)."""
        if not asset_ids:
            return
        ids = self.tag_ids(sorted(set(values)))
        for tag_id in ids:
            self.json("DELETE", f"tags/{tag_id}/assets", json={"ids": list(asset_ids)})

    def remove_from_album(self, name, asset_ids):
        if not asset_ids:
            return
        self.json("DELETE", f"albums/{self.album_id(name)}/assets", json={"ids": list(asset_ids)})

    def tag(self, asset_ids, values):
        ids = self.tag_ids(sorted(set(values)))
        if ids and asset_ids:
            self.json("PUT", "tags/assets", json={"tagIds": ids, "assetIds": list(asset_ids)})


class ImmichML:
    """Text embeddings from the Immich ML /predict endpoint (SigLIP text tower)."""

    def __init__(self, base_url, model, transport=None, timeout=300):
        self.model = model
        self.http = httpx.Client(base_url=base_url.rstrip("/") + "/", timeout=timeout, transport=transport)

    def close(self):
        self.http.close()

    def _predict(self, entries, *, text=None, image=None):
        files = {"image": ("image", image, "application/octet-stream")} if image is not None else None
        data = {"entries": json.dumps(entries)}
        if text is not None:
            data["text"] = text
        response = self.http.post("predict", data=data, files=files)
        if response.is_error:
            raise ImmichError(f"Immich ML predict failed (HTTP {response.status_code})", response.status_code)
        value = response.json()["clip"]
        return json.loads(value) if isinstance(value, str) else value

    def embed_text(self, text):
        return self._predict({"clip": {"textual": {"modelName": self.model}}}, text=text)

    def embed_image(self, data):
        return self._predict({"clip": {"visual": {"modelName": self.model}}}, image=data)


class ImmichDB:
    """Read-only queries of Immich's Postgres."""

    def __init__(self, url):
        if not url:
            raise ImmichError("IMMICH_DB_URL (or the compose DB_PASSWORD) is required")
        self.url = url

    def _conn(self):
        import psycopg
        from psycopg.rows import dict_row

        conn = psycopg.connect(conninfo=self.url, row_factory=dict_row, autocommit=True)
        conn.execute("SET default_transaction_read_only = on")
        return conn

    def job_status(self, asset_ids):
        """{asset id: {metadata, faces, ocr, clip}} with booleans for each finished job."""
        if not asset_ids:
            return {}
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT a.id::text id, j."metadataExtractedAt" IS NOT NULL metadata,'
                ' j."facesRecognizedAt" IS NOT NULL faces, j."ocrAt" IS NOT NULL ocr,'
                " EXISTS (SELECT 1 FROM smart_search s WHERE s.\"assetId\"=a.id) clip"
                " FROM asset a LEFT JOIN asset_job_status j ON j.\"assetId\"=a.id"
                " WHERE a.id = ANY(%s::uuid[])",
                (list(asset_ids),),
            ).fetchall()
        return {r["id"]: r for r in rows}

    def clip_vectors(self, asset_ids):
        """{asset id: [floats]} of the SigLIP image embeddings."""
        if not asset_ids:
            return {}
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT "assetId"::text id, embedding::text v FROM smart_search WHERE "assetId" = ANY(%s::uuid[])',
                (list(asset_ids),),
            ).fetchall()
        return {r["id"]: json.loads(r["v"]) for r in rows}

    def ocr_boxes(self, asset_id):
        with self._conn() as conn:
            return conn.execute(
                'SELECT x1,y1,x2,y2,x3,y3,x4,y4,"boxScore","textScore",text FROM asset_ocr WHERE "assetId"=%s AND "isVisible"',
                (asset_id,),
            ).fetchall()
