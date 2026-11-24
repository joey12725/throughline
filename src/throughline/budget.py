"""Per-request resource budgets (CST-4): latency, tokens, model cost, turns.

A caller may only lower what its token allows. One BudgetTracker is shared by
every statement, leg and model call of a request.
"""

import json
import math
import threading
import time
from dataclasses import asdict, dataclass

# Used when the token row has no such column (before its migration) or no value.
DEFAULT_DEADLINE_MS = 30000
DEFAULT_TOKENS = 50000
DEFAULT_MODEL_COST = 0.10
DEFAULT_CONTROLLER_TURNS = 3
DEFAULT_MODEL_CALLS = 32


@dataclass(frozen=True)
class Budget:
    """None means 'not requested': clamp() fills it from the token's limit."""

    deadline_ms: int | None = None
    tokens: int | None = None
    model_cost: float | None = None
    model_calls: int | None = None
    controller_turns: int | None = None

    def to_dict(self):
        return asdict(self)


def _limit(token_row, column, default):
    value = token_row.get(column) if token_row is not None else None
    return default if value is None else value


def _lower(requested, limit, convert):
    limit = convert(limit)
    if requested is None:
        return limit
    return max(convert(0), min(convert(requested), limit))


def clamp(budget, token_row):
    """The budget a request really gets: never above the token's limits."""
    budget = budget or Budget()
    return Budget(
        deadline_ms=_lower(
            budget.deadline_ms,
            _limit(token_row, "max_deadline_ms", DEFAULT_DEADLINE_MS),
            int,
        ),
        tokens=_lower(
            budget.tokens, _limit(token_row, "max_return_tokens", DEFAULT_TOKENS), int
        ),
        model_cost=_lower(
            budget.model_cost,
            _limit(token_row, "max_model_cost", DEFAULT_MODEL_COST),
            float,
        ),
        model_calls=_lower(budget.model_calls, DEFAULT_MODEL_CALLS, int),
        controller_turns=_lower(
            budget.controller_turns,
            _limit(token_row, "max_controller_turns", DEFAULT_CONTROLLER_TURNS),
            int,
        ),
    )


def estimate_tokens(value):
    """ceil(characters / 4); non-strings are measured as compact JSON."""
    if not isinstance(value, str):
        value = json.dumps(value, default=str, ensure_ascii=False)
    return math.ceil(len(value) / 4)


class BudgetTracker:
    """Thread-safe spending against one clamped Budget."""

    def __init__(self, budget, clock=time.monotonic):
        self.budget = budget
        self._clock = clock
        self._started = clock()
        self._lock = threading.Lock()
        self._tokens = 0
        self._cost = 0.0
        self._calls = 0
        self._turns = 0

    def elapsed_ms(self):
        return (self._clock() - self._started) * 1000

    def remaining_ms(self):
        """Milliseconds left before the deadline (None when there is none)."""
        if self.budget.deadline_ms is None:
            return None
        return max(0.0, self.budget.deadline_ms - self.elapsed_ms())

    def expired(self):
        remaining = self.remaining_ms()
        return remaining is not None and remaining <= 0

    def reserve(self, cost=0.0, calls=1):
        """Reserve model spend before a call; False (nothing spent) if over."""
        cost = float(cost or 0)
        with self._lock:
            if (
                self.budget.model_cost is not None
                and self._cost + cost > self.budget.model_cost + 1e-12
            ):
                return False
            if (
                self.budget.model_calls is not None
                and self._calls + calls > self.budget.model_calls
            ):
                return False
            self._cost += cost
            self._calls += calls
            return True

    def release(self, cost=0.0, calls=1):
        """Give back a reservation whose call did not happen."""
        with self._lock:
            self._cost = max(0.0, self._cost - float(cost or 0))
            self._calls = max(0, self._calls - calls)

    def remaining_tokens(self):
        if self.budget.tokens is None:
            return None
        with self._lock:
            return max(0, self.budget.tokens - self._tokens)

    def spend_tokens(self, count):
        """Count returned tokens; False (nothing spent) when they do not fit."""
        with self._lock:
            if (
                self.budget.tokens is not None
                and self._tokens + count > self.budget.tokens
            ):
                return False
            self._tokens += count
            return True

    def take_turn(self):
        """Count one controller turn; False when none is left."""
        with self._lock:
            if (
                self.budget.controller_turns is not None
                and self._turns >= self.budget.controller_turns
            ):
                return False
            self._turns += 1
            return True

    def snapshot(self):
        remaining = self.remaining_ms()
        with self._lock:
            return dict(
                limits=self.budget.to_dict(),
                spent=dict(
                    ms=round(self.elapsed_ms(), 3),
                    tokens=self._tokens,
                    model_cost=round(self._cost, 6),
                    model_calls=self._calls,
                    controller_turns=self._turns,
                ),
                remaining_ms=None if remaining is None else round(remaining, 3),
                expired=remaining is not None and remaining <= 0,
            )
