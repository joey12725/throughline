"""Channel roles: what a channel is for, as declared by its plugin.

Core never keys behaviour on a channel's name. A plugin declares, per channel,
an optional ``role`` (what kind of records the channel holds), an optional
``orient_priority`` (higher sorts first in POST /v1/orient) and an optional
``context_window_min`` (the smallest thread window POST /v1/find reads around
a hit when the caller did not choose one). A channel that declares nothing is
treated neutrally: no orient section picks it up by role, it sorts after
prioritised channels, and find uses the requested window.

Roles are an open vocabulary (lowercase words); the ones core reads are in
KNOWN_ROLES. Roles are always resolved over the channels the caller can see,
so an asker never learns about a channel through its role.

Legacy fallback. Installs that predate roles have no declarations. When none
of the caller-visible channels declares a role, roles are inferred from the
naming conventions older core releases hard-coded, all in LEGACY_CONVENTIONS
below and nowhere else. As soon as any visible channel declares a role, the
table is ignored and only declarations count.
"""

from dataclasses import dataclass
from fnmatch import fnmatchcase

# Roles core gives behaviour to. Other roles are accepted and stored.
CHAT = "chat"
CALENDAR = "calendar"
TELEMETRY = "telemetry"
TRANSCRIPT = "transcript"
VOICE_SUMMARY = "voice_summary"
LOCATION = "location"
EMAIL = "email"
KNOWN_ROLES = (CHAT, CALENDAR, TELEMETRY, TRANSCRIPT, VOICE_SUMMARY, LOCATION, EMAIL)

# Roles whose latest record orient shows under recent_telemetry.
ORIENT_STATE_ROLES = (TELEMETRY, LOCATION)

ROLE_PATTERN = r"^[a-z][a-z0-9_]{0,39}$"

# --- Legacy naming conventions (installs without role declarations) ---------
# (glob over the channel name, role, orient_priority, context_window_min).
# First match wins. These reproduce exactly what older releases hard-coded in
# orient and find for the channel names of the original connectors; new
# plugins declare roles instead and are never matched here once any visible
# channel declares one.
LEGACY_CONVENTIONS = (
    ("health.oura.daily", TELEMETRY, 110, None),
    ("health.oura.sleep", TELEMETRY, 109, None),
    ("health.oura.workout", TELEMETRY, 108, None),
    ("health.weight", TELEMETRY, 107, None),
    ("morning.weather", TELEMETRY, 106, None),
    ("morning.finance", TELEMETRY, 105, None),
    ("morning.tasks", TELEMETRY, 104, None),
    ("morning.health", TELEMETRY, 103, None),
    ("morning.calendar", TELEMETRY, 102, None),
    ("location.timeline", LOCATION, 101, None),
    ("location.places", LOCATION, 100, None),
    ("health.*", TELEMETRY, None, None),
    ("location.*", LOCATION, None, None),
    # Other digests of the morning pipeline were never shown by orient.
    ("morning.*", None, None, None),
    ("calendar.events", CALENDAR, None, None),
    ("recordings.wave", VOICE_SUMMARY, None, None),
    ("transcripts.wave", TRANSCRIPT, None, 6),
    ("chat.*", CHAT, None, None),
    ("email.*", EMAIL, None, None),
)


@dataclass(frozen=True)
class ChannelRole:
    name: str
    role: str | None = None
    orient_priority: int | None = None
    context_window_min: int | None = None
    # True when the role comes from a declaration, not the legacy table.
    declared: bool = False


def _legacy(name):
    for pattern, role, priority, window in LEGACY_CONVENTIONS:
        if fnmatchcase(name, pattern):
            return role, priority, window
    return None, None, None


def resolve(rows):
    """{name: ChannelRole} from caller-visible channel rows.

    rows: mappings with name and, when stored, role, orient_priority and
    context_window_min. Declared values always win; the legacy conventions
    fill in only when no row declares a role.
    """
    rows = [dict(r) for r in rows if r.get("name")]
    legacy = not any(r.get("role") for r in rows)
    out = {}
    for r in rows:
        role, priority, window = (
            _legacy(r["name"]) if legacy else (None, None, None)
        )
        out[r["name"]] = ChannelRole(
            name=r["name"],
            role=r.get("role") or role,
            orient_priority=(
                r["orient_priority"]
                if r.get("orient_priority") is not None
                else priority
            ),
            context_window_min=(
                r["context_window_min"]
                if r.get("context_window_min") is not None
                else window
            ),
            declared=bool(r.get("role")),
        )
    return out


def load(names, *, scope, token):
    """Resolve the roles of the given channel names, reading only what the
    caller's scope shows in api.channels (a hidden channel is never listed)."""
    from .query import query_ex
    from .retrieval.scope_sql import lit

    names = sorted({n for n in names if isinstance(n, str) and n})
    if not names:
        return {}
    try:
        rows = query_ex(
            "SELECT name, role, orient_priority, context_window_min FROM api.channels"
            f" WHERE name = ANY({lit(names, 'text[]')})",
            len(names) + 5,
            scope=scope,
            token_row=token,
            materialize=False,
        )["rows"]
    except Exception:
        # A database that predates roles (or a scope that cannot read
        # api.channels) falls back to names alone.
        rows = [dict(name=n) for n in names]
    seen = {r["name"] for r in rows}
    # A name the caller sees records of but whose channel row is not listed
    # still resolves (neutral, or by legacy convention).
    rows = list(rows) + [dict(name=n) for n in names if n not in seen]
    return resolve(rows)


def with_role(roles, *wanted):
    """Names of channels holding one of the wanted roles, highest
    orient_priority first, then by name."""
    picked = [r for r in roles.values() if r.role in wanted]
    return [
        r.name
        for r in sorted(
            picked,
            key=lambda r: (-(r.orient_priority if r.orient_priority is not None else -10**9), r.name),
        )
    ]
