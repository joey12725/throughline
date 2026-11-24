"""Plugin-managed, content-addressed asset store and outcome cache.

Core has no asset store yet. Files are stored as ``<sha256>.<ext>`` and linked
as ``throughline-asset://<namespace>/<sha256>.<ext>`` so that a later core asset
endpoint can serve the same names. Each plugin passes its own namespace (Beeper
keeps ``beeper``, the YouTube connector uses ``youtube``). The store is a small
interface; ``LocalAssetStore`` is the only implementation.
"""

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path

SCHEME = "throughline-asset://"


def base_url_for(namespace):
    return SCHEME + namespace


def default_state_dir(name):
    """Per-plugin state directory: %LOCALAPPDATA% or the XDG state home."""
    if os.name == "nt":
        base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.getenv("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "throughline" / name


def extension(value):
    value = re.sub(r"[^a-z0-9]", "", (value or "").lower())[:10]
    return value or "bin"


class StoreError(OSError):
    """The asset store cannot write (disk full, permissions).

    Never recorded as a per-file failure: the run stops and the same work is
    retried on the next run instead of being cached as permanently failed.
    """


class AssetStore:
    """Interface: put bytes, get a stable link; look up stored files."""

    namespace = "media"
    base_url = None

    def put(self, data, ext):
        """Store data; return {sha256, ext, bytes, link}."""
        raise NotImplementedError

    def path(self, link):
        """Local path of a stored asset given its link, or None."""
        raise NotImplementedError

    def link(self, sha256, ext):
        base = self.base_url or base_url_for(self.namespace)
        return base.rstrip("/") + "/" + sha256 + "." + extension(ext)


class LocalAssetStore(AssetStore):
    def __init__(self, directory, base_url=None, namespace="media"):
        self.directory = Path(directory)
        self.namespace = namespace
        self.base_url = base_url or base_url_for(namespace)

    def put(self, data, ext):
        ext = extension(ext)
        digest = hashlib.sha256(data).hexdigest()
        target = self.directory / (digest + "." + ext)
        if not target.exists():
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                handle, temporary = tempfile.mkstemp(dir=self.directory, suffix=".part")
            except OSError as error:
                raise StoreError(f"asset store unavailable: {error}") from None
            try:
                with os.fdopen(handle, "wb") as out:
                    out.write(data)
                os.replace(temporary, target)
            except BaseException as error:
                Path(temporary).unlink(missing_ok=True)
                if isinstance(error, OSError):
                    raise StoreError(f"asset store write failed: {error}") from None
                raise
        return {
            "sha256": digest,
            "ext": ext,
            "bytes": len(data),
            "link": self.link(digest, ext),
        }

    def path(self, link):
        name = (link or "").rsplit("/", 1)[-1]
        if not re.fullmatch(r"[0-9a-f]{64}\.[a-z0-9]{1,10}", name):
            return None
        candidate = self.directory / name
        return candidate if candidate.exists() else None


class Cache:
    """Enrichment outcomes keyed by a stable source key, in one SQLite file.

    Read-only when this run may not do new work: then the file is never
    created, and a missing file simply means nothing was enriched yet.
    """

    def __init__(self, path, readonly=False):
        self.conn = None
        path = Path(path)
        if readonly:
            if path.exists():
                self.conn = sqlite3.connect(
                    path.resolve().as_uri() + "?mode=ro", uri=True
                )
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS results (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self.conn.commit()

    def get(self, key):
        if self.conn is None:
            return None
        row = self.conn.execute(
            "SELECT value FROM results WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value):
        self.conn.execute(
            "INSERT INTO results (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, sort_keys=True, ensure_ascii=False)),
        )
        self.conn.commit()

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None
