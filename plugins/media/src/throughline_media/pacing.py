"""Enrichment pacing: rolling daily limits and new-inbound-only.

Both are kept in the plugin's outcome cache (the SQLite file in its state
directory), so they hold across separate runs and processes:

- ``usage``: one row per network fetch (or per transcribed second) with its
  time. A daily limit counts the rows of the last 24 hours, so it is a rolling
  window, not a calendar day. A fetch counts when it is attempted, whether or
  not it succeeds.
- ``enabled``: per enrichment kind, the time it was first switched on. Only
  items that arrived at or after that time are enriched unless the backlog is
  requested explicitly.

The transcription endpoint's daily minutes are the exception: they are one
pool for every plugin, kept in a separate file (see ``endpoint_allowance``).

A limit of 0 disables that kind; ``unlimited`` lifts the limit.
"""

import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from .assets import default_state_dir

WINDOW_S = 24 * 60 * 60
UNLIMITED = "unlimited"
# The endpoint's daily minutes live here, shared by every plugin.
STATE_DIR_SETTING = "transcribe_state_dir"
STATE_DIR_ENV = "THROUGHLINE_TRANSCRIBE_STATE_DIR"


def parse_limit(value, name):
    """None for ``unlimited``, else a non-negative int (0 disables)."""
    text = str("" if value is None else value).strip().lower()
    if text == UNLIMITED:
        return None
    if text.isdigit():
        return int(text)
    raise ValueError(f"{name} must be a non-negative integer or 'unlimited'")


def instant(value):
    """Aware UTC datetime from an ISO date/time or datetime; None if unusable."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).strip())
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def parse_since(value, name):
    """The explicit enable time (`--enrich-since`), or None when not given."""
    if value is None or str(value).strip() == "":
        return None
    moment = instant(value)
    if moment is None:
        raise ValueError(f"{name} must be an ISO date or time, e.g. 2026-01-31")
    return moment


def iso(epoch):
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


class Pacing:
    """Daily usage and enable times on the cache's SQLite connection.

    `conn` may be None (nothing cached yet on a read-only run) or read-only;
    then nothing is written: limits read as fully used and enable times
    are only read.
    """

    def __init__(self, conn, *, writable=True, clock=time.time, window=WINDOW_S):
        self.conn = conn
        self.writable = bool(writable and conn is not None)
        self.clock = clock
        self.window = window
        if self.writable:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS usage"
                " (kind TEXT NOT NULL, at REAL NOT NULL, amount REAL NOT NULL)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS usage_kind_at ON usage (kind, at)"
            )
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS enabled"
                " (kind TEXT PRIMARY KEY, since TEXT NOT NULL)"
            )
            self.conn.execute(
                "DELETE FROM usage WHERE at < ?", (self.clock() - 2 * self.window,)
            )
            self.conn.commit()

    # Daily limits ------------------------------------------------------------------

    def _rows(self, kind):
        if self.conn is None:
            return []
        try:
            return self.conn.execute(
                "SELECT at, amount FROM usage WHERE kind=? AND at > ? ORDER BY at",
                (kind, self.clock() - self.window),
            ).fetchall()
        except sqlite3.OperationalError:
            return []  # A read-only cache written before pacing existed.

    def used(self, kind):
        return sum(amount for _, amount in self._rows(kind))

    def remaining(self, kind, limit):
        """What the last 24 hours leave of `limit`; None when unlimited."""
        if limit is None:
            return None
        return max(0.0, limit - self.used(kind))

    def take(self, kind, limit, amount=1):
        """Record one attempt of `amount` if it fits under `limit`; atomic.

        Runs in an immediate transaction, so two processes sharing the state
        directory never both take the last unit.
        """
        if not self.writable or (limit is not None and amount > limit):
            return False
        if self.conn.in_transaction:
            self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            now = self.clock()
            if limit is not None:
                (used,) = self.conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM usage WHERE kind=? AND at > ?",
                    (kind, now - self.window),
                ).fetchone()
                if used + amount > limit:
                    self.conn.rollback()
                    return False
            self.conn.execute(
                "INSERT INTO usage (kind, at, amount) VALUES (?, ?, ?)",
                (kind, now, float(amount)),
            )
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return True

    def frees_at(self, kind, limit, amount=1):
        """ISO time when `amount` fits under `limit` again; None if now or never."""
        if limit is None or amount > limit:
            return None
        rows = self._rows(kind)
        excess = sum(a for _, a in rows) + amount - limit
        if excess <= 0:
            return None
        for at, value in rows:
            excess -= value
            if excess <= 0:
                return iso(at + self.window)
        return None

    # New inbound only --------------------------------------------------------------

    def since(self, kind, explicit=None):
        """When `kind` was first enabled: stored on first use.

        An explicit time (`--enrich-since`) replaces the stored one. Read-only
        runs store nothing and return the stored time, or None.
        """
        if explicit is not None:
            if self.writable:
                self.conn.execute(
                    "INSERT INTO enabled (kind, since) VALUES (?, ?)"
                    " ON CONFLICT (kind) DO UPDATE SET since=excluded.since",
                    (kind, explicit.isoformat()),
                )
                self.conn.commit()
            return explicit
        if self.conn is None:
            return None
        try:
            row = self.conn.execute(
                "SELECT since FROM enabled WHERE kind=?", (kind,)
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row:
            return instant(row[0])
        if not self.writable:
            return None
        now = datetime.fromtimestamp(self.clock(), UTC)
        # Another process may store its time first; then that one holds.
        self.conn.execute(
            "INSERT OR IGNORE INTO enabled (kind, since) VALUES (?, ?)",
            (kind, now.isoformat()),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT since FROM enabled WHERE kind=?", (kind,)
        ).fetchone()
        return instant(row[0])


class Allowance:
    """A daily allowance of transcribed seconds, for the transcriber."""

    KIND = "transcribe_seconds"

    def __init__(self, pacing, minutes, *, owns=False):
        self.pacing = pacing
        self.limit = None if minutes is None else minutes * 60
        self.owns = owns  # Close the connection with the allowance.

    def close(self):
        if self.owns and self.pacing.conn is not None:
            self.pacing.conn.close()
            self.pacing.conn = None
            self.pacing.writable = False

    def capacity_s(self):
        return self.limit

    def remaining_s(self):
        return self.pacing.remaining(self.KIND, self.limit)

    def take(self, seconds):
        return self.pacing.take(self.KIND, self.limit, seconds)

    def frees_at(self, seconds=1):
        return self.pacing.frees_at(self.KIND, self.limit, seconds)


def endpoint_state_dir(settings):
    """Where the endpoint's daily minutes are kept, shared by every plugin."""
    return Path(
        settings.get(STATE_DIR_SETTING)
        or os.getenv(STATE_DIR_ENV)
        or default_state_dir("media")
    )


def endpoint_allowance(settings, minutes, *, writable=True, clock=time.time):
    """The transcription endpoint's daily minutes, one pool on this machine.

    Kept in its own SQLite file (``transcribe.sqlite3``) outside any plugin's
    state directory, so Beeper and the YouTube connector draw on the same
    minutes in any 24 hours. A run that does no new work opens it read-only
    (or not at all when it does not exist yet).
    """
    path = endpoint_state_dir(settings) / "transcribe.sqlite3"
    conn = None
    if writable:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
    elif path.exists():
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    pacing = Pacing(conn, writable=writable, clock=clock)
    return Allowance(pacing, minutes, owns=True)
