"""Identity qualities of a delivered record, from best to worst.

A plugin may say how it identified a record in fields.identity_quality:

  native_id    the source's own stable id
  bridge_id    an id assigned by a bridge or relay in front of the source
  fingerprint  a hash of the record's content and time
  unknown      nothing better

Ingest uses the order to decide whether a first delivery under a new key of
an aliased record creates a version (only when its quality ranks strictly
higher than the current version's); the retrieval dedup stage keeps the
best-ranked record of a set of 'same_as' aliases. Both read this one
definition. THROUGHLINE_IDENTITY_QUALITY_ORDER (comma-separated, best first)
overrides the default.

LEGACY_ALIASES maps quality names that existing installs have already stored
to their generic name, so records delivered before the rename keep their
precedence. An alias named explicitly in the override is taken as written.
"""

import os

DEFAULT_ORDER = ("native_id", "bridge_id", "fingerprint", "unknown")
# The original chat bridge connector wrote its own name for bridge ids.
LEGACY_ALIASES = {"beeper_id": "bridge_id"}


def order():
    """The configured order, best first."""
    configured = tuple(
        part.strip()
        for part in (os.getenv("THROUGHLINE_IDENTITY_QUALITY_ORDER") or "").split(",")
        if part.strip()
    )
    return configured or DEFAULT_ORDER


def _canonical(value, current):
    if isinstance(value, str) and value not in current:
        return LEGACY_ALIASES.get(value, value)
    return value


def position(value, current=None):
    """0 for the best quality; an unlisted or missing quality ranks after
    every listed one (after 'unknown' when it is listed)."""
    current = current or order()
    value = _canonical(value, current)
    if isinstance(value, str) and value in current:
        return current.index(value)
    if "unknown" in current:
        return current.index("unknown") + 1
    return len(current)


def rank(value, current=None):
    """Higher is better; an unlisted or missing quality is 0."""
    current = current or order()
    value = _canonical(value, current)
    if isinstance(value, str) and value in current:
        return len(current) - current.index(value)
    return 0
