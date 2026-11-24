"""Outcome classes shared by every media step."""


class MediaUnavailable(Exception):
    """Permanent: `status` names why (private, dead, too_long, members_only, ...)."""

    def __init__(self, status, reason):
        super().__init__(reason)
        self.status = status


class MediaTransient(Exception):
    """May succeed on a later run: rate limits, login walls, network errors."""


class BudgetExhausted(MediaTransient):
    """This run's transcription allowance is used up; retry on a later run."""
