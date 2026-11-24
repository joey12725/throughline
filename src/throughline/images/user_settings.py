"""The owner's image settings: tl.image_settings (migration 0065).

    read(conn) -> dict          # the row, or DEFAULTS before 0065 (available False)
    write(conn, owner, changes) -> dict
    mode_for(settings, unrestricted) -> 'hide' | 'blur' | 'show'

Before migration 0065 the table does not exist: read() answers the defaults
(today's behaviour) with available=False and write() refuses, so the
pipeline and the routes work on both sides of the migration. Detection uses
to_regclass, never a failing statement (that would abort the caller's
transaction).
"""

from psycopg.rows import dict_row

DEFAULTS = {
    "nsfw_mode": "hide",
    "nsfw_threshold": 0.6,
    "unscanned": "hide",
    "restricted_nsfw_mode": "hide",
    "shared_previews": "include",
    "location_fallback": "throughline_and_immich",
    "location_window_min": 30,
    "location_max_accuracy_m": 500,
}
CHOICES = {
    "nsfw_mode": ("hide", "blur", "show"),
    "unscanned": ("hide", "show"),
    "restricted_nsfw_mode": ("hide", "blur", "show"),
    "shared_previews": ("include", "exclude"),
    "location_fallback": ("off", "throughline", "throughline_and_immich"),
}
RANGES = {
    "location_window_min": (1, 1440),
    "location_max_accuracy_m": (1, 100000),
}
FIELDS = tuple(DEFAULTS)


def _cur(conn):
    return conn.cursor(row_factory=dict_row)


def available(conn):
    """Whether migration 0065 is applied (tl.image_settings exists)."""
    row = _cur(conn).execute("SELECT to_regclass('tl.image_settings') IS NOT NULL AS ok").fetchone()
    return bool(row and row["ok"])


def read(conn=None):
    """The settings with available (0065 applied) and updated_at; DEFAULTS
    when the table or its row is missing."""
    if conn is None:
        from ..db import connect

        with connect("app") as c:
            return read(c)
    out = dict(DEFAULTS, available=False, updated_at=None)
    if not available(conn):
        return out
    row = _cur(conn).execute(
        f"SELECT {','.join(FIELDS)},updated_at FROM tl.image_settings WHERE one"
    ).fetchone()
    out["available"] = True
    if row:
        out.update(row)
        out["nsfw_threshold"] = round(float(out["nsfw_threshold"]), 4)
    return out


def public(settings):
    """The settings as GET /v1/images/settings answers them."""
    return {k: settings[k] for k in (*FIELDS, "updated_at")}


def mode_for(settings, unrestricted):
    """The nsfw mode of an asker: nsfw_mode, or restricted_nsfw_mode."""
    return settings["nsfw_mode"] if unrestricted else settings["restricted_nsfw_mode"]


def parse(key, value):
    """A value from text (the CLI's key=value) or JSON, checked; ValueError otherwise."""
    if key not in DEFAULTS:
        raise ValueError(f"unknown image setting {key!r}; one of {', '.join(FIELDS)}")
    if key in CHOICES:
        if not isinstance(value, str) or value not in CHOICES[key]:
            raise ValueError(f"{key} must be one of {', '.join(CHOICES[key])}")
        return value
    if key == "nsfw_threshold":
        if isinstance(value, bool):
            raise ValueError("nsfw_threshold must be a number above 0 and at most 1")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("nsfw_threshold must be a number above 0 and at most 1") from None
        if not 0 < number <= 1:
            raise ValueError("nsfw_threshold must be a number above 0 and at most 1")
        return number
    low, high = RANGES[key]
    if isinstance(value, bool):
        raise ValueError(f"{key} must be an integer from {low} to {high}")
    try:
        number = int(str(value).strip()) if isinstance(value, str) else value
    except ValueError:
        raise ValueError(f"{key} must be an integer from {low} to {high}") from None
    if not isinstance(number, int) or not low <= number <= high:
        raise ValueError(f"{key} must be an integer from {low} to {high}")
    return number


def write(conn, owner, changes):
    """Change some settings (owner only). Unknown keys and bad values are a
    ValueError; before 0065 a LookupError. Returns the settings after."""
    if not owner:
        raise PermissionError("only the owner changes image settings")
    if not isinstance(changes, dict):
        raise ValueError("settings must be an object")
    values = {k: parse(k, v) for k, v in changes.items()}
    if not available(conn):
        raise LookupError("image settings need migration 0065 (throughline migrate)")
    if values:
        sets = ",".join(f"{k}=%s" for k in values)
        with conn.transaction():
            row = conn.execute(
                f"UPDATE tl.image_settings SET {sets},updated_at=now() WHERE one RETURNING one",
                tuple(values.values()),
            ).fetchone()
        if row is None:
            raise LookupError("image settings are missing; run tl.reseed()")
    return read(conn)
