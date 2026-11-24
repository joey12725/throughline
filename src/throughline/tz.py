"""The owner's time zone: one setting for every feature.

THROUGHLINE_TIMEZONE holds an IANA zone name (for example Europe/Lisbon).
THROUGHLINE_OWNER_TZ is its deprecated older name, read only when the new one
is unset; a warning is logged once when it is used.
"""

import logging
import os

DEPRECATED = "THROUGHLINE_OWNER_TZ"
SETTING = "THROUGHLINE_TIMEZONE"
_warned = False


def name():
    """The configured zone name, or '' when neither variable is set."""
    global _warned
    value = (os.getenv(SETTING) or "").strip()
    if value:
        return value
    value = (os.getenv(DEPRECATED) or "").strip()
    if value and not _warned:
        _warned = True
        logging.getLogger(__name__).warning(
            "%s is deprecated; set %s instead", DEPRECATED, SETTING
        )
    return value


def zone(default="UTC"):
    """The configured zone as a ZoneInfo; `default` when unset or invalid."""
    from zoneinfo import ZoneInfo

    value = name()
    try:
        return ZoneInfo(value) if value else ZoneInfo(default)
    except Exception:
        return ZoneInfo(default)
