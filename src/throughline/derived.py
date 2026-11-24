"""Deferred thread maintenance: numbering and conversations of large threads.

Ingest marks a large thread dirty instead of renumbering and regrouping it on
every batch. Readers stay correct without this module: api.message_versions
computes exact numbers for dirty threads and tl.live_conversations hides their
stored conversations. Materializing only makes conversations available again.

Rules kept here:
* one thread per transaction, holding one channel lock, so no lock is held
  across threads, channels or network calls;
* the read path never waits for an ingest lock, never waits longer than a
  bounded time, and never fails because maintenance failed.
"""

import logging
import os
import threading

import psycopg

from .db import connect

log = logging.getLogger("throughline.derived")


def dirty_threads():
    with connect("app") as conn:
        return [
            r["thread_id"]
            for r in conn.execute(
                "SELECT thread_id FROM tl.dirty_threads ORDER BY thread_id"
            )
        ]


def materialize(wait=False):
    """Materialize dirty threads, each in its own short transaction.

    wait=False skips a thread whose channel is being ingested; it stays dirty
    and is picked up by the next call.
    """
    done = skipped = 0
    for tid in dirty_threads():
        try:
            with connect("app") as conn:
                ok = conn.execute(
                    "SELECT tl.refresh_thread(%s,%s) ok", (tid, wait)
                ).fetchone()["ok"]
        except psycopg.Error as exc:
            # One broken thread must not starve the others.
            log.warning("thread %s was not materialized: %s", tid, exc)
            ok = False
        if ok:
            done += 1
        else:
            skipped += 1
    return dict(done=done, skipped=skipped)


_cond = threading.Condition()
_requested = 0
_completed = 0
_running = False


def _drain():
    global _completed, _running
    while True:
        with _cond:
            target = _requested
        try:
            materialize(wait=False)
        except Exception as exc:  # A reader must never fail on maintenance.
            log.warning("deferred thread maintenance failed: %s", exc)
        with _cond:
            _completed = target
            _cond.notify_all()
            if _requested == target:
                _running = False
                return


def read_wait_s():
    try:
        return max(0.0, float(os.getenv("THROUGHLINE_READ_MATERIALIZE_WAIT_S", "2")))
    except ValueError:
        return 2.0


def materialize_for_read(timeout=None):
    """Give pending maintenance a bounded chance to finish before a read.

    Work runs on one background thread per process and continues after the
    reader stops waiting, so a thread too large for the wait is still finished
    once rather than restarted by every read. Returns whether maintenance
    known to this call completed in time.
    """
    global _requested, _running
    try:
        with connect("app") as conn:
            dirty = conn.execute(
                "SELECT EXISTS(SELECT 1 FROM tl.dirty_threads) dirty"
            ).fetchone()["dirty"]
    except psycopg.Error as exc:
        log.warning("deferred thread maintenance check failed: %s", exc)
        return False
    if not dirty:
        return True
    with _cond:
        _requested += 1
        mine = _requested
        if not _running:
            _running = True
            threading.Thread(
                target=_drain, name="throughline-derived", daemon=True
            ).start()
        return _cond.wait_for(
            lambda: _completed >= mine,
            read_wait_s() if timeout is None else timeout,
        )
