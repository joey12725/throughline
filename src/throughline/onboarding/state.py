"""The owner's progress through onboarding (tl.setup_state, one row)."""

from __future__ import annotations

from psycopg.types.json import Jsonb

from ..db import connect
from .errors import SetupError
from .owner import owner_ready

STEPS = ("welcome", "account", "models", "photos", "sources", "connect", "companion", "import", "people",
         "lenses", "pages", "queries", "agent", "tour")


def _row(conn):
    row = conn.execute("SELECT * FROM tl.setup_state WHERE id=1").fetchone()
    if row is None:
        row = conn.execute("INSERT INTO tl.setup_state(id) VALUES (1) ON CONFLICT DO NOTHING RETURNING *").fetchone() or conn.execute(
            "SELECT * FROM tl.setup_state WHERE id=1"
        ).fetchone()
    return row


def public(row):
    ready, hint = owner_ready()
    return dict(
        status=row["status"], mode=row["mode"], steps=row["steps"] or {}, choices=row["choices"] or {},
        owner_ready=ready, owner_hint=hint, started_at=row["started_at"], completed_at=row["completed_at"],
    )


def get():
    with connect("app") as conn:
        return public(_row(conn))


def save(patch):
    patch = patch or {}
    with connect("app") as conn:
        row = _row(conn)
        steps = dict(row["steps"] or {})
        for step, value in (patch.get("steps") or {}).items():
            if step not in STEPS:
                raise SetupError("Unknown step", f"{step!r} is not an onboarding step.", "Use one of: " + ", ".join(STEPS))
            if not isinstance(value, dict) or value.get("status") not in ("todo", "done", "skipped"):
                raise SetupError("Bad step status", f"{step}: status must be todo, done or skipped.")
            steps[step] = {"status": value["status"], "at": value.get("at")}
        choices = dict(row["choices"] or {})
        choices.update(patch.get("choices") or {})
        mode = patch.get("mode") or row["mode"]
        if mode not in ("fresh", "restore", "migrate"):
            raise SetupError("Bad mode", "mode must be fresh, restore or migrate.")
        row = conn.execute(
            "UPDATE tl.setup_state SET steps=%s,choices=%s,mode=%s,"
            "status=CASE WHEN status='done' THEN 'done' ELSE 'in_progress' END,"
            "started_at=coalesce(started_at,now()),updated_at=now() WHERE id=1 RETURNING *",
            (Jsonb(steps), Jsonb(choices), mode),
        ).fetchone()
        return public(row)


def complete():
    with connect("app") as conn:
        _row(conn)
        row = conn.execute(
            "UPDATE tl.setup_state SET status='done',completed_at=now(),started_at=coalesce(started_at,now()),"
            "updated_at=now() WHERE id=1 RETURNING *"
        ).fetchone()
        return public(row)


def reset():
    with connect("app") as conn:
        _row(conn)
        row = conn.execute(
            "UPDATE tl.setup_state SET status='in_progress',steps='{}',completed_at=NULL,updated_at=now() WHERE id=1 RETURNING *"
        ).fetchone()
        return public(row)


def mark_restored(*, keep_steps=True):
    """Mark this install as restored while keeping onboarding open to add more
    sources. Preserves existing choices and connected data."""
    from . import store

    marker = store.setup_dir() / "restored"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("restored\n", encoding="utf-8")
    with connect("app") as conn:
        _row(conn)
        if keep_steps:
            row = conn.execute(
                "UPDATE tl.setup_state SET mode='restore',status='in_progress',"
                "started_at=coalesce(started_at,now()),completed_at=NULL,updated_at=now() "
                "WHERE id=1 RETURNING *"
            ).fetchone()
        else:
            row = conn.execute(
                "UPDATE tl.setup_state SET mode='restore',status='in_progress',steps='{}',"
                "started_at=coalesce(started_at,now()),completed_at=NULL,updated_at=now() "
                "WHERE id=1 RETURNING *"
            ).fetchone()
        return public(row)
