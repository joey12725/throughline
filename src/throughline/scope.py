"""Who is asking: the scope every read runs under.

A Scope is built once per request from the authenticated token and is the only
thing query.reader_transaction accepts. It is turned into transaction settings
that the gate functions in the database read; nothing else decides visibility.
"""

import os
from dataclasses import dataclass

MODES = ("owner", "asker", "deny")


def default_unknown_asker():
    """How an owner token's unregistered asker is treated: unrestricted | deny."""
    value = os.getenv("THROUGHLINE_UNKNOWN_ASKER", "unrestricted").strip().lower()
    return value if value in ("unrestricted", "deny") else "deny"


@dataclass(frozen=True)
class Scope:
    token_id: int | None
    token_kind: str
    asker: str | None
    mode: str
    investigation_id: str | None = None
    # What an asker without a tl.askers row may see: unrestricted | deny.
    unknown_asker: str = "deny"

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError("scope mode must be owner, asker or deny")
        if self.unknown_asker not in ("unrestricted", "deny"):
            raise ValueError("unknown_asker must be unrestricted or deny")
        if self.mode == "asker" and not self.asker:
            raise ValueError("an asker scope needs an asker")

    @classmethod
    def from_token(cls, token, investigation_id=None):
        """Scope of an authenticated token row (with its bound 'asker').

        owner token: owner, or asker when it names one (an unregistered asker
        follows THROUGHLINE_UNKNOWN_ASKER, default unrestricted).
        plugin token: owner. agent token with an empty askers list: owner (the
        legacy full-read token). agent token with a list: asker, and an
        unregistered asker is denied. Anything else fails closed.
        """
        kind = token.get("kind")
        asker = token.get("asker") or None
        common = dict(
            token_id=token.get("id"),
            token_kind=kind,
            investigation_id=investigation_id,
        )
        if kind == "owner":
            if asker:
                return cls(
                    asker=asker,
                    mode="asker",
                    unknown_asker=default_unknown_asker(),
                    **common,
                )
            return cls(asker=None, mode="owner", **common)
        if kind == "plugin":
            return cls(asker=None, mode="owner", **common)
        if kind == "agent":
            allowed = list(token.get("askers") or [])
            if not allowed:
                return cls(asker=None, mode="owner", **common)
            if asker and asker in allowed:
                return cls(asker=asker, mode="asker", unknown_asker="deny", **common)
        return cls(asker=None, mode="deny", **common)

    @classmethod
    def local(cls):
        """In-process owner scope (CLI and library calls without a token)."""
        return cls(token_id=None, token_kind="local", asker=None, mode="owner")

    def gucs(self):
        """Transaction settings placed in every reader transaction."""
        return {
            "throughline.scope_mode": self.mode,
            "throughline.asker": self.asker or "",
            "throughline.token_id": "" if self.token_id is None else str(self.token_id),
            "throughline.unknown_asker": self.unknown_asker,
            "throughline.investigation": self.investigation_id or "",
        }
