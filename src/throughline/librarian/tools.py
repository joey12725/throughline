"""The librarian's four tools (LIB-3, LIB-5, LIB-6).

Each tool is a call any agent can make over HTTP with the same token:

    describe(subject)                       GET  /v1/describe
    execute(plan, dry_run=False)            POST /v1/plans
    inspect(handle, view, selector, budget) POST /v1/sets/{handle}/inspect
    deliver(selection, notes, unresolved)   POST /v1/packets

There is no tool to call a librarian, change budgets, write data or reach the
network: the controller can only read under the caller's scope, write the
caller's own result sets (through plans) and ask for a packet. Every read
runs under the investigation's Scope (the caller's scope plus the
investigation id), so the database gate applies before the controller sees
anything.
"""

from psycopg import sql as pgsql
from psycopg.types.json import Jsonb

from .. import packets, plans, resultsets
from ..db import connect

NAMES = ("describe", "execute", "inspect", "deliver")


def record_items(view):
    """How many records (items naming a record_id) a view shows."""
    items = (view or {}).get("items") or []
    return sum(1 for item in items if isinstance(item, dict) and item.get("record_id"))


class Tools:
    """The tool belt of one investigation; the controller never sees more."""

    def __init__(self, scope, token_row, *, tracker, trace, investigation_id):
        self._scope = scope
        self._token_row = token_row
        self._tracker = tracker
        self._trace = trace
        self._investigation_id = investigation_id
        self.inspected = 0
        self.handles = set()

    # -- describe -------------------------------------------------------------
    def describe(self, subject=None):
        """api.describe(subject) under the scope (a hidden channel reads as
        an unknown one)."""
        from ..query import query_ex

        statement = (
            pgsql.SQL("SELECT api.describe({}) AS d")
            .format(pgsql.Literal(subject))
            .as_string()
        )
        result = query_ex(statement, 1, scope=self._scope)
        if self._trace is not None:
            self._trace.event("librarian.describe", statement, rows=1)
        return result["rows"][0]["d"]

    # -- execute --------------------------------------------------------------
    def _remaining(self):
        """What is left of the investigation's budget, for one plan."""
        snapshot = self._tracker.snapshot()
        limits, spent = snapshot["limits"], snapshot["spent"]
        remaining = self._tracker.remaining_ms()

        def left(name, key):
            limit = limits.get(name)
            return None if limit is None else max(0, limit - spent[key])

        return dict(
            deadline_ms=None if remaining is None else max(1, int(remaining)),
            tokens=left("tokens", "tokens"),
            model_cost=left("model_cost", "model_cost"),
            model_calls=left("model_calls", "model_calls"),
            controller_turns=0,
        )

    def execute(self, plan, dry_run=False):
        """Validate and run one plan under what is left of the budget; its
        statements land in the investigation's trace. Raises ValueError for
        an invalid plan (including a 'librarian' node)."""
        checked = plans.validate(plan)
        result = plans.execute(
            self._scope,
            self._token_row,
            checked.plan,
            budgets=self._remaining(),
            dry_run=dry_run,
            trace=self._trace,
        )
        spent = ((result.get("execution") or {}).get("budget") or {}).get("spent") or {}
        # The plan ran on its own tracker: charge what it spent to ours.
        self._tracker.reserve(float(spent.get("model_cost") or 0), 0)
        calls = int(spent.get("model_calls") or 0)
        if calls:
            self._tracker.reserve(0.0, calls)
        self.handles.update(result.get("handles", {}).values())
        return result

    # -- inspect --------------------------------------------------------------
    def inspect(self, handle, view="summary", selector=None, token_budget=1000):
        """A bounded view of a set; counts.inspected_by_controller grows by
        the records the view shows."""
        found = resultsets.inspect(self._scope, handle, view, selector, token_budget)
        shown = record_items(found)
        self.inspected += shown
        if shown:
            self._count(handle, shown)
        if self._trace is not None:
            self._trace.event(
                "librarian.inspect",
                None,
                rows=shown,
                detail=dict(handle=handle, view=view),
            )
        return found

    def _count(self, handle, shown):
        try:
            with connect("app") as conn:
                conn.execute(
                    "UPDATE tl.result_sets SET counts=coalesce(counts,'{}'::jsonb)"
                    " || jsonb_build_object('inspected_by_controller',"
                    " coalesce((counts->>'inspected_by_controller')::bigint,0)+%s)"
                    " WHERE handle=%s AND token_id IS NOT DISTINCT FROM %s"
                    " AND asker IS NOT DISTINCT FROM %s",
                    (shown, handle, self._scope.token_id, self._scope.asker or None),
                )
        except Exception:  # A count must never fail the investigation.
            pass

    # -- deliver --------------------------------------------------------------
    def deliver(
        self,
        selection_handle,
        orientation_notes=None,
        unresolved_needs=None,
        token_budget=None,
    ):
        """The evidence packet of one of the investigation's sets; records are
        copied by the runtime from a live read, notes need verified refs."""
        packet = packets.build(
            self._scope,
            selection_handle,
            notes=list(orientation_notes or []),
            unresolved=list(unresolved_needs or []),
            token_budget=token_budget,
            investigation=self._investigation_id,
        )
        if self._trace is not None:
            self._trace.event(
                "librarian.deliver",
                None,
                rows=len(packet["records"]),
                detail=dict(
                    handle=selection_handle,
                    notes_kept=len(packet["orientation"]),
                ),
            )
        return packet


def as_json(value):
    """Jsonb of a JSON-safe value (for the investigation row)."""
    return Jsonb(value)
